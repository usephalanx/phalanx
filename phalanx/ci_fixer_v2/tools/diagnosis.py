"""Diagnosis tools — read-only capabilities the agent uses to understand
the failure before deciding what to fix.

Implemented so far:
  Week 1.5:  fetch_ci_log
  Week 1.6a: get_pr_context, get_pr_diff, query_fingerprint

Pending (Week 1.6b):
  - get_ci_history, git_blame

Design notes:
  - These tools do NOT reach into the DB or HTTP layer directly. Everything
    that touches external state goes through a module-level seam function
    (e.g. `_call_github_api`, `_load_fingerprint_row`) so tests can
    monkeypatch without docker, httpx live traffic, or a real database.
  - API keys and repo identity are read from AgentContext, never hard-coded.
"""

from __future__ import annotations

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
# fetch_ci_log
# ─────────────────────────────────────────────────────────────────────────────

FETCH_CI_LOG_SCHEMA = ToolSchema(
    name="fetch_ci_log",
    description=(
        "Fetch the raw log of a failing CI job. Use this early in the run to "
        "understand the failure. Returns the relevant error section (already "
        "cleaned of timestamps, ANSI codes, and known noise lines). "
        "Supported providers: github_actions, circleci, buildkite. The agent "
        "normally only needs to call this once per run."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": (
                    "Provider-specific identifier of the failed job. For GitHub "
                    "Actions, this is the check_run_id (also called job_id). "
                    "For CircleCI, the workflow_id. For Buildkite, the build_id."
                ),
            },
            "failed_jobs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional — names of the failed jobs (improves log section "
                    "extraction when the full log zip is used)."
                ),
            },
        },
        "required": ["job_id"],
    },
)


# Test seam: fetches the log via the v1 adapter. Tests monkeypatch this.
async def _fetch_log_via_v1(
    provider: str,
    repo_full_name: str,
    build_id: str,
    failed_jobs: list[str],
    pr_number: int | None,
    api_key: str,
) -> str:
    """Thin wrapper over phalanx.ci_fixer.log_fetcher for v2. Tests
    patch this symbol directly so no real HTTP is required.
    """
    from phalanx.ci_fixer.events import CIFailureEvent
    from phalanx.ci_fixer.log_fetcher import get_log_fetcher

    event = CIFailureEvent(
        provider=provider,
        repo_full_name=repo_full_name,
        branch="",
        commit_sha="",
        build_id=build_id,
        build_url="",
        failed_jobs=list(failed_jobs),
        pr_number=pr_number,
        raw_payload={},
    )
    fetcher = get_log_fetcher(provider)
    return await fetcher.fetch(event, api_key)


async def _compute_and_persist_fingerprint(
    ctx: AgentContext, log_text: str
) -> str | None:
    """Best-effort fingerprint side-effect from fetch_ci_log.

    Parses the fetched log via v1's log_parser, computes the stable
    sha256[:16] identity via the shared `compute_fingerprint` helper,
    writes it to AgentContext AND to CIFixRun.fingerprint_hash in DB.

    Any failure here is LOGGED, not raised — the agent can still run
    without fingerprint memory (query_fingerprint will miss, the run
    behaves like a fresh failure). Returns the hash on success or
    None on failure / when ctx already has one set.
    """
    if ctx.fingerprint_hash:
        return ctx.fingerprint_hash
    try:
        from phalanx.agents.ci_fixer import compute_fingerprint
        from phalanx.ci_fixer.log_parser import parse_log

        parsed = parse_log(log_text)
        fingerprint = compute_fingerprint(parsed)
    except Exception as exc:
        log.warning(
            "v2.tools.fetch_ci_log.fingerprint_compute_failed",
            error=str(exc),
        )
        return None

    ctx.fingerprint_hash = fingerprint

    try:
        await _persist_fingerprint_to_ci_fix_run(ctx.ci_fix_run_id, fingerprint)
    except Exception as exc:
        # DB write failure is non-fatal — ctx carries the fingerprint
        # for in-memory use; v1 subsystems (outcome_tracker, pattern
        # promoter) that key off CIFixRun.fingerprint_hash won't see it,
        # but that only degrades post-run learning — not this run's fix.
        log.warning(
            "v2.tools.fetch_ci_log.fingerprint_persist_failed",
            ci_fix_run_id=ctx.ci_fix_run_id,
            error=str(exc),
        )
    return fingerprint


async def _persist_fingerprint_to_ci_fix_run(
    ci_fix_run_id: str, fingerprint_hash: str
) -> None:
    """Write fingerprint_hash back to CIFixRun. Test seam."""
    from sqlalchemy import update

    from phalanx.db.models import CIFixRun
    from phalanx.db.session import get_db

    async with get_db() as session:
        await session.execute(
            update(CIFixRun)
            .where(CIFixRun.id == ci_fix_run_id)
            .values(fingerprint_hash=fingerprint_hash)
        )
        await session.commit()


async def _handle_fetch_ci_log(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    job_id = tool_input.get("job_id")
    if not job_id:
        return ToolResult(ok=False, error="job_id is required")
    if not ctx.ci_api_key:
        return ToolResult(
            ok=False,
            error=(
                "ci_api_key is not set on AgentContext — run bootstrap must "
                "resolve it from CIIntegration before the agent runs."
            ),
        )

    try:
        text = await _fetch_log_via_v1(
            provider=ctx.ci_provider,
            repo_full_name=ctx.repo_full_name,
            build_id=str(job_id),
            failed_jobs=list(tool_input.get("failed_jobs") or []),
            pr_number=ctx.pr_number,
            api_key=ctx.ci_api_key,
        )
    except KeyError as exc:
        # get_log_fetcher raises KeyError for unknown providers.
        return ToolResult(ok=False, error=f"unsupported_provider: {exc}")
    except Exception as exc:  # defensive: never let tool failures crash the loop
        log.warning(
            "v2.tools.fetch_ci_log.error",
            provider=ctx.ci_provider,
            job_id=job_id,
            error=str(exc),
        )
        return ToolResult(ok=False, error=f"fetch_failed: {exc}")

    # Side-effect: compute + persist fingerprint so query_fingerprint and
    # v1 post-run subsystems can find this run in memory.
    fingerprint = await _compute_and_persist_fingerprint(ctx, text)

    return ToolResult(
        ok=True,
        data={
            "log_text": text,
            "provider": ctx.ci_provider,
            "job_id": job_id,
            "char_count": len(text),
            "fingerprint_hash": fingerprint or "",
        },
    )


class _FetchCILogTool:
    schema = FETCH_CI_LOG_SCHEMA
    handler = staticmethod(_handle_fetch_ci_log)


_fetch_ci_log_tool = _FetchCILogTool()
register(_fetch_ci_log_tool)


# ─────────────────────────────────────────────────────────────────────────────
# Shared seams for GitHub-API-backed tools + DB-backed tools
# ─────────────────────────────────────────────────────────────────────────────


async def _call_github_api(
    path: str, api_key: str, accept: str = "application/vnd.github+json"
) -> tuple[int, str, Any]:
    """Test seam for GitHub REST calls. Tests patch this symbol on the
    `diagnosis` module rather than on `_github_api`, so each test only
    affects its own tool.
    """
    from phalanx.ci_fixer_v2.tools._github_api import github_get

    return await github_get(path, api_key, accept=accept)


async def _load_fingerprint_row(
    repo_full_name: str, fingerprint_hash: str
) -> Any:
    """Test seam for the CIFailureFingerprint lookup. Returns a row-like
    object with attribute access (SQLAlchemy row or SimpleNamespace in
    tests), or None if no match.
    """
    from sqlalchemy import select

    from phalanx.db.models import CIFailureFingerprint
    from phalanx.db.session import get_db

    async with get_db() as session:
        result = await session.execute(
            select(CIFailureFingerprint).where(
                CIFailureFingerprint.repo_full_name == repo_full_name,
                CIFailureFingerprint.fingerprint_hash == fingerprint_hash,
            )
        )
        return result.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# get_pr_context
# ─────────────────────────────────────────────────────────────────────────────

GET_PR_CONTEXT_SCHEMA = ToolSchema(
    name="get_pr_context",
    description=(
        "Fetch PR metadata: title, body, labels, author, head branch, base "
        "branch, state, timestamps, and has_write_permission (whether "
        "Phalanx can commit directly to the author's branch). Use this "
        "early in the run to understand the intent and scope of the PR."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pr_number": {
                "type": "integer",
                "description": (
                    "PR number. Defaults to the PR that triggered the CI "
                    "failure (AgentContext.pr_number) if omitted."
                ),
            },
        },
        "required": [],
    },
)


async def _handle_get_pr_context(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    pr_number = tool_input.get("pr_number") or ctx.pr_number
    if not pr_number:
        return ToolResult(
            ok=False,
            error="pr_number is required (and AgentContext.pr_number is unset)",
        )
    if not ctx.ci_api_key:
        return ToolResult(ok=False, error="ci_api_key is not set on AgentContext")

    try:
        status, _text, body = await _call_github_api(
            f"/repos/{ctx.repo_full_name}/pulls/{pr_number}",
            ctx.ci_api_key,
        )
    except Exception as exc:
        log.warning("v2.tools.get_pr_context.error", error=str(exc))
        return ToolResult(ok=False, error=f"github_call_failed: {exc}")

    if status != 200 or not isinstance(body, dict):
        return ToolResult(
            ok=False,
            error=f"github_api_error: status={status}",
        )

    labels = [
        (lbl.get("name") or "") for lbl in (body.get("labels") or []) if lbl
    ]
    return ToolResult(
        ok=True,
        data={
            "pr": {
                "number": body.get("number"),
                "title": body.get("title") or "",
                "body": body.get("body") or "",
                "state": body.get("state"),
                "author": (body.get("user") or {}).get("login") or "",
                "head_branch": (body.get("head") or {}).get("ref") or "",
                "base_branch": (body.get("base") or {}).get("ref") or "",
                "labels": labels,
                "created_at": body.get("created_at") or "",
                "updated_at": body.get("updated_at") or "",
            },
            "has_write_permission": ctx.has_write_permission,
        },
    )


class _GetPRContextTool:
    schema = GET_PR_CONTEXT_SCHEMA
    handler = staticmethod(_handle_get_pr_context)


_get_pr_context_tool = _GetPRContextTool()
register(_get_pr_context_tool)


# ─────────────────────────────────────────────────────────────────────────────
# get_pr_diff
# ─────────────────────────────────────────────────────────────────────────────

GET_PR_DIFF_SCHEMA = ToolSchema(
    name="get_pr_diff",
    description=(
        "Fetch the unified diff of the PR plus per-file change stats "
        "(additions, deletions). Use this to see exactly what the author "
        "changed — critical for judging whether the CI failure is caused "
        "by this PR or was preexisting."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pr_number": {
                "type": "integer",
                "description": (
                    "PR number. Defaults to the triggering PR "
                    "(AgentContext.pr_number) if omitted."
                ),
            },
        },
        "required": [],
    },
)


async def _handle_get_pr_diff(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    pr_number = tool_input.get("pr_number") or ctx.pr_number
    if not pr_number:
        return ToolResult(
            ok=False,
            error="pr_number is required (and AgentContext.pr_number is unset)",
        )
    if not ctx.ci_api_key:
        return ToolResult(ok=False, error="ci_api_key is not set on AgentContext")

    # Call 1 — raw diff as plain text
    try:
        diff_status, diff_text, _ = await _call_github_api(
            f"/repos/{ctx.repo_full_name}/pulls/{pr_number}",
            ctx.ci_api_key,
            accept="application/vnd.github.diff",
        )
    except Exception as exc:
        log.warning("v2.tools.get_pr_diff.diff_error", error=str(exc))
        return ToolResult(ok=False, error=f"diff_fetch_failed: {exc}")
    if diff_status != 200:
        return ToolResult(
            ok=False,
            error=f"github_api_error_on_diff: status={diff_status}",
        )

    # Call 2 — per-file stats (paginated at 100/page; MVP fetches first page)
    try:
        files_status, _files_text, files_body = await _call_github_api(
            f"/repos/{ctx.repo_full_name}/pulls/{pr_number}/files?per_page=100",
            ctx.ci_api_key,
        )
    except Exception as exc:
        log.warning("v2.tools.get_pr_diff.files_error", error=str(exc))
        return ToolResult(ok=False, error=f"files_fetch_failed: {exc}")
    if files_status != 200 or not isinstance(files_body, list):
        return ToolResult(
            ok=False,
            error=f"github_api_error_on_files: status={files_status}",
        )

    files_changed = [
        {
            "path": f.get("filename") or "",
            "additions": int(f.get("additions") or 0),
            "deletions": int(f.get("deletions") or 0),
            "status": f.get("status") or "",
        }
        for f in files_body
        if isinstance(f, dict)
    ]
    return ToolResult(
        ok=True,
        data={
            "diff": diff_text,
            "files_changed": files_changed,
            "file_count": len(files_changed),
        },
    )


class _GetPRDiffTool:
    schema = GET_PR_DIFF_SCHEMA
    handler = staticmethod(_handle_get_pr_diff)


_get_pr_diff_tool = _GetPRDiffTool()
register(_get_pr_diff_tool)


# ─────────────────────────────────────────────────────────────────────────────
# query_fingerprint (Tier-1 memory lookup)
# ─────────────────────────────────────────────────────────────────────────────

QUERY_FINGERPRINT_SCHEMA = ToolSchema(
    name="query_fingerprint",
    description=(
        "Tier-1 memory lookup: 'have we seen this exact failure class in "
        "this repo before, and what fix has worked / not worked?' Returns "
        "seen_count, success_count, failure_count, and the last known-good "
        "patch (if any). Use this early — if a proven fix exists, prefer "
        "it over inventing a new approach."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fingerprint_hash": {
                "type": "string",
                "description": (
                    "16-char stable identity of the failure class. Defaults "
                    "to AgentContext.fingerprint_hash (seeded by the run "
                    "bootstrap) if omitted."
                ),
            },
        },
        "required": [],
    },
)


async def _handle_query_fingerprint(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    fingerprint_hash = tool_input.get("fingerprint_hash") or ctx.fingerprint_hash
    if not fingerprint_hash:
        return ToolResult(
            ok=False,
            error=(
                "fingerprint_hash is required (not in tool input and "
                "AgentContext.fingerprint_hash is unset)"
            ),
        )

    try:
        row = await _load_fingerprint_row(ctx.repo_full_name, fingerprint_hash)
    except Exception as exc:
        log.warning(
            "v2.tools.query_fingerprint.db_error",
            repo=ctx.repo_full_name,
            error=str(exc),
        )
        return ToolResult(ok=False, error=f"db_query_failed: {exc}")

    if row is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "fingerprint_hash": fingerprint_hash,
            },
        )

    last_seen = getattr(row, "last_seen_at", None)
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "fingerprint_hash": fingerprint_hash,
            "tool": getattr(row, "tool", None) or "",
            "sample_errors": getattr(row, "sample_errors", None) or "",
            "seen_count": int(getattr(row, "seen_count", 0) or 0),
            "success_count": int(getattr(row, "success_count", 0) or 0),
            "failure_count": int(getattr(row, "failure_count", 0) or 0),
            "last_good_patch_json": (
                getattr(row, "last_good_patch_json", None) or ""
            ),
            "last_good_tool_version": (
                getattr(row, "last_good_tool_version", None) or ""
            ),
            "last_seen_at": last_seen.isoformat() if last_seen else "",
        },
    )


class _QueryFingerprintTool:
    schema = QUERY_FINGERPRINT_SCHEMA
    handler = staticmethod(_handle_query_fingerprint)


_query_fingerprint_tool = _QueryFingerprintTool()
register(_query_fingerprint_tool)


# ─────────────────────────────────────────────────────────────────────────────
# get_ci_history  (Week 1.6b)
# ─────────────────────────────────────────────────────────────────────────────
# Spec note: flake_rate >= 0.2 is the threshold at which the failure is
# "plausibly flaky, not a real regression." The agent uses this to decide
# between rerun, fix test isolation, or real code fix.


# GitHub conclusion strings mapped to our simplified status.
_PASSED_CONCLUSIONS = frozenset({"success"})
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "startup_failure"})


def _map_conclusion(conclusion: str | None) -> str:
    if conclusion in _PASSED_CONCLUSIONS:
        return "passed"
    if conclusion in _FAILED_CONCLUSIONS:
        return "failed"
    return "other"  # skipped / cancelled / neutral / None


GET_CI_HISTORY_SCHEMA = ToolSchema(
    name="get_ci_history",
    description=(
        "Fetch recent CI runs on the default branch for flake detection. "
        "Returns the last N days of workflow runs with pass/fail outcomes "
        "and the flake_rate (failed / (passed + failed)). A flake_rate "
        ">= 0.2 is a strong signal the failure is flaky rather than a real "
        "regression. Optionally filter by `test_identifier` (substring "
        "match on workflow or commit-message content)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Look-back window in days (default 14, max 90).",
                "minimum": 1,
                "maximum": 90,
            },
            "branch": {
                "type": "string",
                "description": "Branch name. Default 'main'.",
            },
            "test_identifier": {
                "type": "string",
                "description": (
                    "Optional substring filter applied to workflow name + "
                    "commit message. When empty, all runs in window are counted."
                ),
            },
        },
        "required": [],
    },
)


async def _handle_get_ci_history(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    if not ctx.ci_api_key:
        return ToolResult(ok=False, error="ci_api_key is not set on AgentContext")

    days_in = tool_input.get("days") or 14
    days = max(1, min(int(days_in), 90))
    branch = tool_input.get("branch") or "main"
    test_identifier = (tool_input.get("test_identifier") or "").strip().lower()

    # GitHub's runs endpoint supports `created` with date operators.
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    path = (
        f"/repos/{ctx.repo_full_name}/actions/runs"
        f"?branch={branch}&status=completed&per_page=50&created=%3E{since}"
    )

    try:
        status, _text, body = await _call_github_api(path, ctx.ci_api_key)
    except Exception as exc:
        log.warning("v2.tools.get_ci_history.error", error=str(exc))
        return ToolResult(ok=False, error=f"github_call_failed: {exc}")
    if status != 200 or not isinstance(body, dict):
        return ToolResult(ok=False, error=f"github_api_error: status={status}")

    raw_runs = body.get("workflow_runs") or []
    runs: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for r in raw_runs:
        if not isinstance(r, dict):
            continue
        if test_identifier:
            name = (r.get("name") or "").lower()
            msg = ((r.get("head_commit") or {}).get("message") or "").lower()
            if test_identifier not in name and test_identifier not in msg:
                continue
        mapped = _map_conclusion(r.get("conclusion"))
        if mapped == "passed":
            passed += 1
        elif mapped == "failed":
            failed += 1
        runs.append(
            {
                "sha": r.get("head_sha") or "",
                "status": mapped,
                "ran_at": r.get("created_at") or "",
                "workflow_name": r.get("name") or "",
                "url": r.get("html_url") or "",
            }
        )

    total_counted = passed + failed
    flake_rate = (failed / total_counted) if total_counted else 0.0

    return ToolResult(
        ok=True,
        data={
            "runs": runs,
            "total": len(runs),
            "passed": passed,
            "failed": failed,
            "flake_rate": round(flake_rate, 3),
            "branch": branch,
            "days": days,
        },
    )


class _GetCIHistoryTool:
    schema = GET_CI_HISTORY_SCHEMA
    handler = staticmethod(_handle_get_ci_history)


_get_ci_history_tool = _GetCIHistoryTool()
register(_get_ci_history_tool)


# ─────────────────────────────────────────────────────────────────────────────
# git_blame  (Week 1.6b)
# ─────────────────────────────────────────────────────────────────────────────

GIT_BLAME_SCHEMA = ToolSchema(
    name="git_blame",
    description=(
        "Standard git blame for a file + line range in the repository "
        "workspace. Returns one record per line: the commit sha that last "
        "touched it, the author, the authoring date, and the commit "
        "summary. Use this to understand WHY a failing line exists — who "
        "added it and with what intent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Path relative to the repo workspace root.",
            },
            "line_start": {
                "type": "integer",
                "description": "1-indexed start line.",
                "minimum": 1,
            },
            "line_end": {
                "type": "integer",
                "description": "1-indexed end line (inclusive, default = line_start).",
                "minimum": 1,
            },
        },
        "required": ["file", "line_start"],
    },
)


# Test seam: run `git blame` subprocess and return raw porcelain output.
async def _run_git_blame(
    workspace: str, file_path: str, line_start: int, line_end: int
) -> tuple[int, str, str]:
    """Return (exit_code, stdout, stderr) from `git blame -L s,e --porcelain`."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        workspace,
        "blame",
        "-L",
        f"{line_start},{line_end}",
        "--porcelain",
        file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # pragma: no cover
            pass
        return (124, "", "git blame timed out")
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


def _parse_blame_porcelain(stdout: str) -> list[dict[str, Any]]:
    """Parse git-blame porcelain output into {line, sha, author, date, summary}."""
    from datetime import datetime, timezone

    lines: list[dict[str, Any]] = []
    headers: dict[str, str] = {}
    current_sha: str | None = None
    current_final_lineno: int | None = None
    seen_shas: dict[str, dict[str, str]] = {}

    for raw in stdout.splitlines():
        if not raw:
            continue
        # Header line: "<sha> <orig> <final> [<num_lines>]"
        if raw and raw[0].isalnum() and " " in raw and len(raw.split()[0]) == 40:
            parts = raw.split()
            current_sha = parts[0]
            current_final_lineno = int(parts[2])
            # Each sha's metadata only repeats on first encounter; cache.
            headers = dict(seen_shas.get(current_sha, {}))
            continue
        # Content line starts with TAB in porcelain.
        if raw.startswith("\t"):
            if current_sha is None or current_final_lineno is None:
                continue
            author = headers.get("author") or "unknown"
            author_time = headers.get("author-time")
            summary = headers.get("summary") or ""
            try:
                if author_time:
                    date_iso = datetime.fromtimestamp(
                        int(author_time), tz=timezone.utc
                    ).isoformat()
                else:
                    date_iso = ""
            except (ValueError, OSError):
                date_iso = ""
            lines.append(
                {
                    "line": current_final_lineno,
                    "sha": current_sha,
                    "author": author,
                    "date": date_iso,
                    "summary": summary,
                }
            )
            # Persist the metadata for this sha so subsequent mentions reuse it.
            if current_sha not in seen_shas:
                seen_shas[current_sha] = dict(headers)
            continue
        # Key-value metadata line before the content.
        if " " in raw:
            key, _, val = raw.partition(" ")
            headers[key] = val
        else:
            headers[raw] = ""
    return lines


async def _handle_git_blame(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    file_path = tool_input.get("file")
    line_start = tool_input.get("line_start")
    if not file_path or not isinstance(file_path, str):
        return ToolResult(ok=False, error="file is required")
    if not isinstance(line_start, int) or line_start < 1:
        return ToolResult(ok=False, error="line_start must be a positive integer")
    line_end_raw = tool_input.get("line_end")
    line_end = (
        line_end_raw if isinstance(line_end_raw, int) and line_end_raw >= line_start
        else line_start
    )

    # Path safety: reuse reading._resolve_in_workspace.
    from phalanx.ci_fixer_v2.tools.reading import _resolve_in_workspace

    resolved = _resolve_in_workspace(ctx.repo_workspace_path, file_path)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return ToolResult(
            ok=False,
            error=f"path_outside_workspace_or_missing: {file_path!r}",
        )

    try:
        exit_code, stdout, stderr = await _run_git_blame(
            ctx.repo_workspace_path, file_path, line_start, line_end
        )
    except FileNotFoundError:
        return ToolResult(ok=False, error="git_binary_missing")

    if exit_code != 0:
        return ToolResult(
            ok=False,
            error=f"git_blame_failed: {stderr.strip() or f'exit_code={exit_code}'}",
        )

    parsed = _parse_blame_porcelain(stdout)
    return ToolResult(
        ok=True,
        data={
            "file": file_path,
            "line_start": line_start,
            "line_end": line_end,
            "lines": parsed,
        },
    )


class _GitBlameTool:
    schema = GIT_BLAME_SCHEMA
    handler = staticmethod(_handle_git_blame)


_git_blame_tool = _GitBlameTool()
register(_git_blame_tool)
