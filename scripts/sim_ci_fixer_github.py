#!/usr/bin/env python3
"""
CI Fixer prod-parity simulation — GitHub Actions target (trigger.dev).

Only difference from a real webhook trigger:
  - PR + failing check-run are discovered via GitHub API and stored to disk
  - Logs are read from scripts/sim_logs_github/ (fetched once, reused offline)
  - CIFixerAgent runs in-process instead of via Celery

Everything else is real:
  - Real git clone + checkout of the failing commit
  - Real sandbox Docker containers
  - Real LLM (Claude) analyst call
  - Real ruff/mypy validation in sandbox
  - Real pytest regression verification in sandbox
  - Real git commit + push (author_branch strategy for lint-only)

Usage:
    # Discover + fetch logs from GitHub, then simulate:
    FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py --fetch

    # Re-run from cached logs (no GitHub API calls):
    FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py

    # Dry-run (skip git push, still real clone/sandbox/LLM):
    FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py --dry-run

    # Override target repo (default: triggerdotdev/trigger.dev):
    FORGE_WORKER=1 python scripts/sim_ci_fixer_github.py --repo owner/repo --fetch
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog

log = structlog.get_logger("sim_ci_fixer_github")

SIM_LOGS_DIR = Path(__file__).parent / "sim_logs_github"
META_FILE = SIM_LOGS_DIR / "meta.json"
COMBINED_LOG_FILE = SIM_LOGS_DIR / "combined_failure_log.txt"

# Failure categories that are worth attempting to auto-fix (lint/format)
_FIXABLE_CONCLUSIONS = {"failure", "timed_out", "cancelled"}
_FIXABLE_NAMES = ("lint", "typecheck", "format", "ruff", "mypy", "eslint", "tsc", "check")


async def _find_failing_pr(repo: str, github_token: str) -> dict:
    """
    Find a recently-failed PR check run in the target repo.
    Returns a meta dict with pr_number, head_sha, branch, check_run_id, job_id.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # List open PRs
        log.info("sim.github.listing_prs", repo=repo)
        r = await client.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            params={"state": "open", "per_page": 30, "sort": "updated", "direction": "desc"},
        )
        r.raise_for_status()
        prs = r.json()
        log.info("sim.github.prs_found", count=len(prs))

        for pr in prs:
            pr_number = pr["number"]
            head_sha = pr["head"]["sha"]
            branch = pr["head"]["ref"]

            # Get check runs for this commit
            r2 = await client.get(
                f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs",
                headers=headers,
                params={"per_page": 50},
            )
            if r2.status_code != 200:
                continue

            runs = r2.json().get("check_runs", [])
            failed = [
                cr for cr in runs
                if cr.get("conclusion") in _FIXABLE_CONCLUSIONS
                or (cr.get("status") == "completed" and cr.get("conclusion") == "failure")
            ]

            # Prefer runs whose name contains a lint/format keyword
            preferred = [
                cr for cr in failed
                if any(kw in cr.get("name", "").lower() for kw in _FIXABLE_NAMES)
            ]
            candidates = preferred if preferred else failed

            if not candidates:
                continue

            cr = candidates[0]
            check_run_id = cr["id"]
            log.info(
                "sim.github.found_failing_pr",
                pr=pr_number,
                branch=branch,
                check_run=cr["name"],
                conclusion=cr["conclusion"],
                sha=head_sha[:12],
            )

            # Try to find the GH Actions job ID (needed for log fetch)
            # The check_run external_id is usually the job run_id; job ID is different
            job_id = None
            try:
                r3 = await client.get(
                    f"https://api.github.com/repos/{repo}/actions/runs/{cr.get('details_url','').split('/')[-1]}/jobs",
                    headers=headers,
                )
                # Simpler: use check_run id directly — GH log endpoint accepts check_run id as job_id
                job_id = check_run_id
            except Exception:
                job_id = check_run_id

            return {
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "branch": branch,
                "check_run_id": check_run_id,
                "check_run_name": cr["name"],
                "conclusion": cr["conclusion"],
                "job_id": job_id,
                "details_url": cr.get("html_url", ""),
            }

    log.error("sim.github.no_failing_pr", repo=repo, hint="No open PRs with failed check runs found")
    sys.exit(1)


async def _fetch_logs_from_github(repo: str, meta: dict, github_token: str) -> str:
    """Fetch failure logs for a specific job from GitHub Actions."""
    from phalanx.ci_fixer.events import CIFailureEvent
    from phalanx.ci_fixer.log_fetcher import GitHubActionsLogFetcher

    event = CIFailureEvent(
        repo_full_name=repo,
        build_id=str(meta["job_id"]),
        branch=meta["branch"],
        commit_sha=meta["head_sha"],
        build_url=meta.get("details_url", ""),
        provider="github_actions",
        pr_number=meta["pr_number"],
    )

    fetcher = GitHubActionsLogFetcher()
    log.info("sim.github.fetching_logs", job_id=meta["job_id"], repo=repo)
    raw_log = await fetcher.fetch(event, api_key=github_token)
    log.info("sim.github.logs_fetched", chars=len(raw_log))
    return raw_log


async def discover_and_fetch(repo: str, github_token: str) -> None:
    """Discover a failing PR and fetch its logs to disk."""
    SIM_LOGS_DIR.mkdir(exist_ok=True)

    meta = await _find_failing_pr(repo, github_token)
    log_text = await _fetch_logs_from_github(repo, meta, github_token)

    META_FILE.write_text(json.dumps(meta, indent=2))
    COMBINED_LOG_FILE.write_text(log_text)

    log.info(
        "sim.github.fetch_done",
        meta=str(META_FILE),
        log_file=str(COMBINED_LOG_FILE),
        pr=meta["pr_number"],
        branch=meta["branch"],
        check_run=meta["check_run_name"],
    )


def _load_meta() -> dict:
    if not META_FILE.exists():
        log.error("sim.meta_missing", path=str(META_FILE), hint="run with --fetch first")
        sys.exit(1)
    return json.loads(META_FILE.read_text())


def _load_log() -> str:
    if not COMBINED_LOG_FILE.exists():
        log.error("sim.log_missing", path=str(COMBINED_LOG_FILE), hint="run with --fetch first")
        sys.exit(1)
    content = COMBINED_LOG_FILE.read_text()
    log.info("sim.log_loaded", path=str(COMBINED_LOG_FILE), chars=len(content))
    return content


async def _setup_integration(session, repo: str) -> str:
    from sqlalchemy import select
    from phalanx.db.models import CIIntegration

    result = await session.execute(
        select(CIIntegration).where(CIIntegration.repo_full_name == repo)
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        log.error(
            "sim.no_integration",
            repo=repo,
            hint="Insert a CIIntegration row first — see _seed_integration() below",
        )
        sys.exit(1)
    log.info("sim.integration_ready", id=str(integration.id)[:8], repo=repo)
    return str(integration.id)


async def _seed_integration(session, repo: str, github_token: str) -> str:
    """Create a CIIntegration row for the target repo if not already present."""
    from sqlalchemy import select
    from phalanx.db.models import CIIntegration

    result = await session.execute(
        select(CIIntegration).where(CIIntegration.repo_full_name == repo)
    )
    integration = result.scalar_one_or_none()
    if integration:
        log.info("sim.integration_exists", id=str(integration.id)[:8])
        return str(integration.id)

    integration = CIIntegration(
        id=str(uuid.uuid4()),
        repo_full_name=repo,
        ci_provider="github_actions",
        github_token=github_token,
        ci_api_key_enc="",
        enabled=True,
    )
    session.add(integration)
    await session.commit()
    log.info("sim.integration_seeded", id=str(integration.id)[:8], repo=repo)
    return str(integration.id)


async def _create_fix_run(session, integration_id: str, meta: dict, github_token: str) -> str:
    from phalanx.db.models import CIFixRun

    run_id = str(uuid.uuid4())
    run = CIFixRun(
        id=run_id,
        integration_id=integration_id,
        repo_full_name=meta["repo"],
        branch=meta["branch"],
        commit_sha=meta["head_sha"],
        pr_number=meta["pr_number"],
        ci_provider="github_actions",
        ci_build_id=str(meta["check_run_id"]),
        build_url=meta.get("details_url", ""),
        failed_jobs=[meta["check_run_name"]],
        failure_summary=f"PR #{meta['pr_number']} check '{meta['check_run_name']}' {meta['conclusion']}",
        failure_category="lint",
        status="QUEUED",
        attempt=1,
    )
    session.add(run)
    await session.commit()
    log.info("sim.fix_run_created", run_id=run_id[:8], pr=meta["pr_number"], branch=meta["branch"])
    return run_id


async def run_simulation(dry_run: bool, github_token: str) -> bool:
    meta = _load_meta()
    raw_log = _load_log()

    log.info(
        "sim.config",
        repo=meta["repo"],
        pr=meta["pr_number"],
        branch=meta["branch"],
        sha=meta["head_sha"][:12],
        check_run=meta["check_run_name"],
        log_chars=len(raw_log),
        dry_run=dry_run,
    )

    from phalanx.db.session import get_db

    async with get_db() as session:
        integration_id = await _seed_integration(session, meta["repo"], github_token)

    async with get_db() as session:
        run_id = await _create_fix_run(session, integration_id, meta, github_token)

    log.info("sim.starting_agent", run_id=run_id[:8])

    # ── Only mock: log fetcher reads from disk (already pre-processed by fetch step) ──
    mock_fetcher = MagicMock()
    mock_fetcher.fetch = AsyncMock(return_value=raw_log)

    # Always suppress PR comments in simulation — never post to external repos
    patch_targets = [
        patch("phalanx.agents.ci_fixer.get_log_fetcher", return_value=mock_fetcher),
        patch(
            "phalanx.agents.ci_fixer.CIFixerAgent._comment_unable_to_fix",
            new_callable=AsyncMock,
        ),
        patch(
            "phalanx.agents.ci_fixer.CIFixerAgent._comment_lint_fix_pushed",
            new_callable=AsyncMock,
        ),
        patch(
            "phalanx.agents.ci_fixer.CIFixerAgent._comment_on_pr",
            new_callable=AsyncMock,
        ),
    ]

    if dry_run:
        from phalanx.ci_fixer.context import VerificationResult

        patch_targets += [
            patch(
                "phalanx.agents.ci_fixer.CIFixerAgent._commit_to_author_branch",
                new_callable=AsyncMock,
                return_value={"sha": "dryrun00", "branch": meta["branch"], "push_failed": False},
            ),
            patch(
                "phalanx.agents.ci_fixer.CIFixerAgent._commit_to_safe_branch",
                new_callable=AsyncMock,
                return_value={"sha": "dryrun0000000000000000000000000000000000", "push_failed": False},
            ),
            patch(
                "phalanx.agents.ci_fixer.CIFixerAgent._open_draft_pr",
                new_callable=AsyncMock,
                return_value=999,
            ),
            patch(
                "phalanx.agents.ci_fixer.VerifierAgent.verify",
                new_callable=AsyncMock,
                return_value=VerificationResult(verdict="passed", output="[dry-run: verification skipped]"),
            ),
        ]

    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patch_targets:
            stack.enter_context(p)
        result = await _run_agent(run_id)

    return result


async def _run_agent(run_id: str) -> bool:
    from phalanx.agents.ci_fixer import CIFixerAgent

    agent = CIFixerAgent(ci_fix_run_id=run_id)
    try:
        result = await agent.execute()
        log.info(
            "sim.agent_done",
            success=result.success,
            output=json.dumps(result.output, indent=2) if result.output else "{}",
        )
        return result.success
    except Exception as exc:
        log.error("sim.agent_failed", error=str(exc))
        import traceback
        traceback.print_exc()
        return False


def _get_github_token() -> str:
    import os
    import subprocess

    # 1. Explicit env var
    token = os.environ.get("GITHUB_TOKEN", "")

    # 2. .env / phalanx settings
    if not token:
        try:
            from phalanx.config.settings import settings
            token = settings.github_token
        except Exception:
            pass

    # 3. gh CLI (no login prompt — just reads cached creds)
    if not token:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=5,
            )
            token = result.stdout.strip()
        except Exception:
            pass

    # 4. macOS keychain (git credential store)
    if not token:
        try:
            result = subprocess.run(
                ["security", "find-internet-password", "-s", "github.com", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            candidate = result.stdout.strip()
            if candidate.startswith("ghp_") or candidate.startswith("gho_") or candidate.startswith("github_pat_"):
                token = candidate
        except Exception:
            pass

    if not token:
        print(
            "\n❌  No GitHub token found. Run once:\n"
            "    make gh-login\n"
            "\n  Or export GITHUB_TOKEN=ghp_... before running.\n"
        )
        sys.exit(1)
    return token


def main():
    parser = argparse.ArgumentParser(description="CI Fixer GitHub Actions simulation")
    parser.add_argument("--fetch", action="store_true", help="Discover failing PR + fetch logs from GitHub")
    parser.add_argument("--dry-run", action="store_true", help="Skip git push (still clones, sandboxes, LLM, validates)")
    parser.add_argument("--repo", default="triggerdotdev/trigger.dev", help="GitHub repo (owner/repo)")
    args = parser.parse_args()

    import os
    os.environ.setdefault("FORGE_WORKER", "1")

    async def _main():
        github_token = _get_github_token()

        if args.fetch:
            log.info("sim.fetching_from_github", repo=args.repo)
            await discover_and_fetch(args.repo, github_token)

        success = await run_simulation(dry_run=args.dry_run, github_token=github_token)
        print(f"\n{'✅ SIMULATION PASSED' if success else '❌ SIMULATION FAILED'}")
        sys.exit(0 if success else 1)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
