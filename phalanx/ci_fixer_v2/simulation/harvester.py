"""GitHub-backed CI fixture harvester.

Crawls public repos' Actions runs, extracts failed runs' logs + PR
context, redacts secrets/PII, and writes each into the corpus layout
(see `fixtures.save_fixture`).

Separation of concerns:
  - `harvest_from_repo`  — the orchestration function (testable).
  - `_call_github_get`   — HTTP seam (tests patch this, no real traffic).
  - CLI: `scripts/harvest_ci_fixtures.py` — thin wrapper that calls here.

Spec §11 guarantees:
  - Every harvested text is passed through `redact` before write.
  - `meta.json` records license + redaction_report.
  - We skip fixtures whose originating repo's license is GPL-class so
    the corpus stays MIT-compatible.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from phalanx.ci_fixer_v2.simulation.fixtures import FixtureMeta, save_fixture
from phalanx.ci_fixer_v2.simulation.redaction import redact

log = structlog.get_logger(__name__)


# ── License gate ──────────────────────────────────────────────────────────
# Harvest-time skip for licenses that are incompatible with our MIT target.
_INCOMPATIBLE_LICENSE_KEYS: frozenset[str] = frozenset(
    {"gpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-2.1", "lgpl-3.0"}
)


@dataclass
class HarvestStats:
    """Summary counters returned at the end of `harvest_from_repo`."""

    total_runs_inspected: int = 0
    fixtures_written: int = 0
    skipped_incompatible_license: int = 0
    skipped_no_log: int = 0
    skipped_no_pr: int = 0
    skipped_errors: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# HTTP seam — tests patch this symbol on the harvester module
# ─────────────────────────────────────────────────────────────────────────────


async def _call_github_get(
    path: str, api_key: str, accept: str = "application/vnd.github+json"
) -> tuple[int, str, Any]:
    """Route through the shared github_api helper."""
    from phalanx.ci_fixer_v2.tools._github_api import github_get

    return await github_get(path, api_key, accept=accept)


# ─────────────────────────────────────────────────────────────────────────────
# Public: harvest_from_repo
# ─────────────────────────────────────────────────────────────────────────────


async def harvest_from_repo(
    repo_full_name: str,
    github_token: str,
    corpus_root: Path,
    language: str,
    failure_class: str,
    days: int = 14,
    limit: int = 10,
) -> HarvestStats:
    """Harvest failed workflow runs from `repo_full_name` into the corpus.

    Args:
        repo_full_name:  'owner/repo' format.
        github_token:    PAT or GitHub App token with read access.
        corpus_root:     Destination root (usually tests/simulation/fixtures).
        language:        Top-level language bucket (must be in LANGUAGES).
        failure_class:   Failure bucket (must be in FAILURE_CLASSES).
        days:            Look-back window.
        limit:           Max fixtures to write from this run.

    Returns HarvestStats with counters suitable for CLI output or tests.
    """
    stats = HarvestStats()
    license_key = await _resolve_repo_license(repo_full_name, github_token)
    if license_key in _INCOMPATIBLE_LICENSE_KEYS:
        stats.skipped_incompatible_license += 1
        log.warning(
            "v2.harvest.skip_license",
            repo=repo_full_name,
            license=license_key,
        )
        return stats

    from datetime import datetime, timedelta, timezone

    since = (
        datetime.now(tz=timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs_path = (
        f"/repos/{repo_full_name}/actions/runs"
        f"?branch=main&status=failure&per_page={min(limit * 2, 100)}&created=%3E{since}"
    )

    status, _text, body = await _call_github_get(runs_path, github_token)
    if status != 200 or not isinstance(body, dict):
        log.error("v2.harvest.runs_fetch_failed", repo=repo_full_name, status=status)
        return stats

    workflow_runs = body.get("workflow_runs") or []
    for run in workflow_runs:
        if stats.fixtures_written >= limit:
            break
        stats.total_runs_inspected += 1
        try:
            written = await _harvest_one_run(
                run=run,
                repo_full_name=repo_full_name,
                github_token=github_token,
                corpus_root=corpus_root,
                language=language,
                failure_class=failure_class,
                license_key=license_key or "unknown",
            )
            if written is None:
                stats.skipped_no_pr += 1
                continue
            stats.fixtures_written += 1
        except _NoLogError:
            stats.skipped_no_log += 1
        except Exception as exc:  # defensive: don't let one failure abort
            stats.skipped_errors += 1
            log.warning(
                "v2.harvest.run_error", repo=repo_full_name, error=str(exc)
            )
        # Be polite with GitHub's rate limit.
        await asyncio.sleep(0.2)

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


class _NoLogError(Exception):
    pass


async def _resolve_repo_license(
    repo_full_name: str, github_token: str
) -> str | None:
    """Look up the repo's license SPDX key; None if not advertised."""
    try:
        status, _text, body = await _call_github_get(
            f"/repos/{repo_full_name}", github_token
        )
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    license_obj = body.get("license") or {}
    key = license_obj.get("key") if isinstance(license_obj, dict) else None
    return str(key).lower() if key else None


async def _harvest_one_run(
    run: dict[str, Any],
    repo_full_name: str,
    github_token: str,
    corpus_root: Path,
    language: str,
    failure_class: str,
    license_key: str,
) -> Path | None:
    """Convert one workflow run into a fixture on disk; return the dir."""
    head_sha = run.get("head_sha") or ""
    pr_numbers = [
        p.get("number")
        for p in (run.get("pull_requests") or [])
        if isinstance(p, dict)
    ]
    if not pr_numbers:
        return None
    pr_number = pr_numbers[0]

    # Fetch PR context
    pr_status, _text, pr_body = await _call_github_get(
        f"/repos/{repo_full_name}/pulls/{pr_number}", github_token
    )
    if pr_status != 200 or not isinstance(pr_body, dict):
        return None

    # Fetch raw diff for PR
    diff_status, diff_text, _ = await _call_github_get(
        f"/repos/{repo_full_name}/pulls/{pr_number}",
        github_token,
        accept="application/vnd.github.diff",
    )
    diff_text = diff_text if diff_status == 200 else ""

    # Fetch run logs (first failed job)
    jobs_status, _text, jobs_body = await _call_github_get(
        f"/repos/{repo_full_name}/actions/runs/{run.get('id')}/jobs",
        github_token,
    )
    if jobs_status != 200 or not isinstance(jobs_body, dict):
        raise _NoLogError("jobs_fetch_failed")
    failed_jobs = [
        j for j in (jobs_body.get("jobs") or [])
        if isinstance(j, dict) and j.get("conclusion") == "failure"
    ]
    if not failed_jobs:
        raise _NoLogError("no_failed_jobs")
    first_failed = failed_jobs[0]
    log_status, log_text, _ = await _call_github_get(
        f"/repos/{repo_full_name}/actions/jobs/{first_failed.get('id')}/logs",
        github_token,
    )
    if log_status != 200 or not log_text:
        raise _NoLogError("log_fetch_failed")

    # Redact everything text-bearing before commit.
    redacted_log, log_report = redact(log_text)
    redacted_diff, diff_report = redact(diff_text)
    redacted_pr_body = {
        "title": pr_body.get("title") or "",
        "body": redact(pr_body.get("body") or "", use_detect_secrets=False)[0],
        "state": pr_body.get("state") or "",
        "author": (pr_body.get("user") or {}).get("login") or "",
        "head_branch": (pr_body.get("head") or {}).get("ref") or "",
        "base_branch": (pr_body.get("base") or {}).get("ref") or "",
        "diff": redacted_diff,
    }

    fixture_id = f"{repo_full_name.replace('/', '-')}-run-{run.get('id')}"
    meta = FixtureMeta(
        fixture_id=fixture_id,
        language=language,
        failure_class=failure_class,
        origin_repo=repo_full_name,
        origin_commit_sha=head_sha,
        origin_pr_number=pr_number,
        license=license_key,
        redaction_report={
            "log": log_report.to_dict(),
            "diff": diff_report.to_dict(),
        },
    )
    return save_fixture(
        root=corpus_root,
        meta=meta,
        raw_log=redacted_log,
        pr_context=redacted_pr_body,
        clone_instructions={
            "repo": repo_full_name,
            "sha": head_sha,
            "branch": redacted_pr_body["head_branch"],
        },
        ground_truth=None,  # resolution commit tracked in a later pass
    )
