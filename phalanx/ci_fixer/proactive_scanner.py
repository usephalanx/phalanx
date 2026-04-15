"""
CI Fix Proactive Scanner — Phase 5.

When a PR is opened (GitHub PR webhook), scan the changed files against the
pattern registry.  If any known bad patterns are detected, post a comment on
the PR before CI even runs.

This gives developers early warning: "Phalanx detected that these changes
match a known CI failure pattern — consider reviewing before pushing."

Architecture:
  1. Webhook receives PR opened event → dispatches scan task
  2. ProactiveScanner fetches changed files from GitHub API
  3. Scans file content against CIPatternRegistry patterns
  4. If findings > 0, posts a comment and records CIProactiveScan row
  5. If CI later fails with the same pattern → CIFixerAgent runs and has history

This is a best-effort system — it NEVER blocks a merge.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime

import structlog

from phalanx.db.models import CIProactiveScan
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)


class ProactiveFinding:
    __slots__ = ("fingerprint_hash", "tool", "description", "severity", "affected_files")

    def __init__(
        self,
        fingerprint_hash: str,
        tool: str,
        description: str,
        severity: str,
        affected_files: list[str],
    ) -> None:
        self.fingerprint_hash = fingerprint_hash
        self.tool = tool
        self.description = description
        self.severity = severity
        self.affected_files = affected_files

    def to_dict(self) -> dict:
        return {
            "fingerprint_hash": self.fingerprint_hash,
            "tool": self.tool,
            "description": self.description,
            "severity": self.severity,
            "affected_files": self.affected_files,
        }


def format_proactive_comment(findings: list[ProactiveFinding], pr_number: int) -> str:
    """Format a GitHub PR comment body for proactive findings."""
    if not findings:
        return ""

    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = len(findings) - warning_count

    header = "## 🔍 Phalanx Pre-CI Scan\n\n"
    if warning_count > 0:
        header += (
            f"Found **{warning_count} pattern(s)** matching known CI failure fingerprints. "
            f"Consider reviewing before CI runs.\n\n"
        )
    else:
        header += (
            f"Found **{info_count} informational pattern(s)** — low severity.\n\n"
        )

    lines = [header, "| Pattern | Tool | Files | Severity |\n", "|---------|------|-------|----------|\n"]
    for f in findings[:10]:
        files_str = ", ".join(f"`{p}`" for p in f.affected_files[:3])
        if len(f.affected_files) > 3:
            files_str += f" (+{len(f.affected_files) - 3} more)"
        severity_icon = "⚠️" if f.severity == "warning" else "ℹ️"
        lines.append(
            f"| {f.description[:60]} | `{f.tool}` | {files_str} | {severity_icon} {f.severity} |\n"
        )

    lines.append(
        "\n---\n*Phalanx pre-scan: these are patterns that have caused CI failures in "
        "this or similar repos. This comment does not block the merge.*"
    )
    return "".join(lines)


def should_post_proactive_comment(findings: list[ProactiveFinding]) -> bool:
    """
    Return True if findings warrant posting a PR comment.

    Only post if there are WARNING-severity findings — info findings are
    too noisy for PR comments.
    """
    return any(f.severity == "warning" for f in findings)


async def _record_scan(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    findings: list[ProactiveFinding],
    comment_posted: bool,
    comment_id: int | None,
    duration_ms: int,
) -> None:
    """Persist a CIProactiveScan row."""
    async with get_db() as session:
        row = CIProactiveScan(
            id=str(uuid.uuid4()),
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
            findings_json=json.dumps([f.to_dict() for f in findings]),
            comment_posted=comment_posted,
            comment_id=comment_id,
            scan_duration_ms=duration_ms,
            scanned_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()


async def scan_pr_for_patterns(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    github_token: str,
) -> list[ProactiveFinding]:
    """
    Scan a PR's changed files against the pattern registry.

    Fetches changed files from GitHub, loads registry patterns for files
    that match known bad fingerprints, returns list of findings.

    This is intentionally simple in Phase 5 — we match on file extension
    and tool type rather than parsing file content (too slow for pre-CI).
    """
    start = time.monotonic()
    findings: list[ProactiveFinding] = []

    try:
        import httpx  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        from phalanx.db.models import CIPatternRegistry  # noqa: PLC0415

        # Fetch changed files from GitHub
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )

        if r.status_code != 200:
            log.warning("proactive_scanner.files_fetch_failed", status=r.status_code)
            return []

        changed_files = [f["filename"] for f in r.json()]
        python_files = [f for f in changed_files if f.endswith(".py")]
        ts_files = [f for f in changed_files if f.endswith((".ts", ".tsx", ".js"))]

        # Load relevant patterns from registry
        async with get_db() as session:
            result = await session.execute(
                select(CIPatternRegistry).where(
                    CIPatternRegistry.tool.in_(["ruff", "mypy", "pytest", "tsc", "eslint"])
                )
            )
            patterns = result.scalars().all()

        for pattern in patterns:
            tool = pattern.tool
            if tool in ("ruff", "mypy", "pytest") and not python_files:
                continue
            if tool in ("tsc", "eslint") and not ts_files:
                continue

            affected = python_files if tool in ("ruff", "mypy", "pytest") else ts_files
            if not affected:
                continue

            # Only warn for patterns with high success count (proven fixes)
            severity = "warning" if pattern.total_success_count >= 5 else "info"
            findings.append(
                ProactiveFinding(
                    fingerprint_hash=pattern.fingerprint_hash,
                    tool=tool,
                    description=pattern.description or f"Known {tool} failure pattern",
                    severity=severity,
                    affected_files=affected[:5],
                )
            )

    except Exception as exc:
        log.warning("proactive_scanner.scan_failed", error=str(exc))

    duration_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "proactive_scanner.done",
        repo=repo_full_name,
        pr=pr_number,
        findings=len(findings),
        duration_ms=duration_ms,
    )
    return findings


# ── Celery task ────────────────────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.ci_fixer.proactive_scanner.scan_pr",
    queue="ci_fixer",
    soft_time_limit=60,
    time_limit=90,
)
def scan_pr_task(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    github_token: str,
) -> None:
    """Celery task: scan a PR for known bad patterns and post comment."""
    try:
        asyncio.run(_run_scan(repo_full_name, pr_number, commit_sha, github_token))
    except Exception:
        log.exception("proactive_scanner.task_unhandled")
        raise


async def _run_scan(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    github_token: str,
) -> None:
    start = time.monotonic()
    findings = await scan_pr_for_patterns(repo_full_name, pr_number, commit_sha, github_token)

    comment_posted = False
    comment_id = None

    if should_post_proactive_comment(findings):
        comment_id = await _post_comment(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            github_token=github_token,
            findings=findings,
        )
        comment_posted = comment_id is not None

    duration_ms = int((time.monotonic() - start) * 1000)

    await _record_scan(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        commit_sha=commit_sha,
        findings=findings,
        comment_posted=comment_posted,
        comment_id=comment_id,
        duration_ms=duration_ms,
    )


async def _post_comment(
    repo_full_name: str,
    pr_number: int,
    github_token: str,
    findings: list[ProactiveFinding],
) -> int | None:
    """Post a GitHub PR comment with the findings. Returns comment ID or None."""
    try:
        import httpx  # noqa: PLC0415

        body = format_proactive_comment(findings, pr_number)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
                json={"body": body},
            )
        if r.status_code in (200, 201):
            return r.json().get("id")
        log.warning("proactive_scanner.comment_failed", status=r.status_code)
    except Exception as exc:
        log.warning("proactive_scanner.comment_error", error=str(exc))
    return None
