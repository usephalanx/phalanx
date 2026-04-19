#!/usr/bin/env python3
"""
CI Fixer prod-parity local simulation.

Only difference from a real webhook trigger:
  - Logs are read from scripts/sim_logs/combined_failure_log.txt (fetched once
    from CircleCI via scripts/fetch_sim_logs.py, then reused offline)
  - CIFixerAgent runs in-process instead of via Celery

Everything else is real:
  - Real git clone + checkout of the failing commit
  - Real sandbox Docker containers
  - Real LLM (Claude) analyst call
  - Real ruff/mypy validation in sandbox
  - Real pytest regression verification in sandbox
  - Real git commit + push to phalanx/ci-fix/* branch
  - Real GitHub PR opened

Usage:
    # Full prod-parity run (pushes a real PR):
    FORGE_WORKER=1 python scripts/sim_ci_fixer.py

    # Dry-run (skips git push + PR, still real clone/sandbox/LLM/validate):
    FORGE_WORKER=1 python scripts/sim_ci_fixer.py --dry-run

    # Re-fetch logs from CircleCI before running:
    FORGE_WORKER=1 python scripts/sim_ci_fixer.py --fetch-logs

    # Override which log file to use:
    FORGE_WORKER=1 python scripts/sim_ci_fixer.py --log-file scripts/sim_logs/job_478_test.txt
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

log = structlog.get_logger("sim_ci_fixer")

# ── Sim log directory ─────────────────────────────────────────────────────────
SIM_LOGS_DIR = Path(__file__).parent / "sim_logs"
META_FILE = SIM_LOGS_DIR / "meta.json"
COMBINED_LOG_FILE = SIM_LOGS_DIR / "combined_failure_log.txt"


def _load_meta() -> dict:
    if not META_FILE.exists():
        log.error("sim.meta_missing", path=str(META_FILE), hint="run with --fetch-logs first")
        sys.exit(1)
    return json.loads(META_FILE.read_text())


def _load_log(log_file: Path) -> str:
    if not log_file.exists():
        log.error("sim.log_missing", path=str(log_file), hint="run with --fetch-logs first")
        sys.exit(1)
    content = log_file.read_text()
    log.info("sim.log_loaded", path=str(log_file), chars=len(content))
    return content


async def _fetch_logs_from_circleci(repo: str, branch: str) -> None:
    """Fetch real CircleCI failure logs and write to sim_logs/."""
    import httpx

    SIM_LOGS_DIR.mkdir(exist_ok=True)

    from phalanx.db.session import get_db
    from sqlalchemy import text

    async with get_db() as session:
        r = await session.execute(
            text("SELECT github_token, ci_api_key_enc FROM ci_integrations WHERE repo_full_name = :r"),
            {"r": repo},
        )
        row = r.one()
        ci_token = row.ci_api_key_enc

    headers = {"Circle-Token": ci_token}
    project_slug = f"github/{repo}"

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"https://circleci.com/api/v2/project/{project_slug}/pipeline",
            params={"branch": branch},
            headers=headers,
        )
        r.raise_for_status()
        pipelines = r.json()["items"]

        for pipeline in pipelines[:10]:
            pid = pipeline["id"]
            r2 = await client.get(f"https://circleci.com/api/v2/pipeline/{pid}/workflow", headers=headers)
            workflows = r2.json().get("items", [])
            failed = [w for w in workflows if w.get("status") in ("failed", "error")]
            if not failed:
                continue

            wf = failed[0]
            wf_id = wf["id"]
            commit_sha = pipeline.get("vcs", {}).get("revision", "")
            log.info("sim.fetch.found_pipeline", pipeline=pid[:8], workflow=wf_id[:8], commit=commit_sha[:12])

            r3 = await client.get(f"https://circleci.com/api/v2/workflow/{wf_id}/job", headers=headers)
            jobs = r3.json().get("items", [])
            failed_jobs = [j for j in jobs if j.get("status") in ("failed", "timedout")]

            all_logs: list[str] = []
            for job in failed_jobs[:3]:
                job_num = job.get("job_number")
                job_name = job["name"]
                if not job_num:
                    continue

                r4 = await client.get(
                    f"https://circleci.com/api/v1.1/project/{project_slug}/{job_num}",
                    headers=headers,
                )
                log_parts: list[str] = []
                if r4.status_code == 200:
                    for step in r4.json().get("steps", []):
                        for action in step.get("actions", []):
                            if action.get("exit_code", 0) != 0 or action.get("failed"):
                                output_url = action.get("output_url")
                                if output_url:
                                    lr = await client.get(output_url)
                                    if lr.status_code == 200:
                                        for item in lr.json():
                                            log_parts.append(item.get("message", ""))
                                log_parts.append(f"\n[Step: {step.get('name','?')} exit={action.get('exit_code','?')}]\n")

                log_text = "".join(log_parts)
                job_file = SIM_LOGS_DIR / f"job_{job_num}_{job_name.replace('/','_')}.txt"
                job_file.write_text(log_text)
                all_logs.append(f"JOB: {job_name}\n{'='*60}\n{log_text}\n")
                log.info("sim.fetch.saved_job", file=str(job_file), chars=len(log_text))

            COMBINED_LOG_FILE.write_text("\n\n---\n\n".join(all_logs))
            meta = {
                "pipeline_id": pid,
                "workflow_id": wf_id,
                "commit_sha": commit_sha,
                "branch": branch,
                "repo": repo,
                "failed_jobs": [j["name"] for j in failed_jobs],
                "pipeline_number": pipeline.get("number"),
            }
            META_FILE.write_text(json.dumps(meta, indent=2))
            log.info("sim.fetch.done", combined=str(COMBINED_LOG_FILE), meta=str(META_FILE))
            return

    log.error("sim.fetch.no_failed_pipeline", branch=branch)
    sys.exit(1)


async def _setup_integration(session, repo: str) -> str:
    from sqlalchemy import select
    from phalanx.db.models import CIIntegration

    result = await session.execute(select(CIIntegration).where(CIIntegration.repo_full_name == repo))
    integration = result.scalar_one_or_none()
    if integration is None:
        log.error("sim.no_integration", repo=repo, hint="CIIntegration row missing — seed it via the API or DB")
        sys.exit(1)
    log.info("sim.integration_ready", id=str(integration.id)[:8], repo=repo)
    return str(integration.id)


async def _create_fix_run(
    session, integration_id: str, meta: dict, commit_sha: str
) -> str:
    from phalanx.db.models import CIFixRun

    run_id = str(uuid.uuid4())
    run = CIFixRun(
        id=run_id,
        integration_id=integration_id,
        repo_full_name=meta["repo"],
        branch=meta["branch"],
        commit_sha=commit_sha,
        ci_provider="circleci",
        ci_build_id=meta["workflow_id"],
        build_url=f"https://app.circleci.com/pipelines/github/{meta['repo']}/{meta['pipeline_number']}",
        failed_jobs=meta["failed_jobs"],
        failure_summary=f"Pipeline {meta['pipeline_number']} failed on branch {meta['branch']}",
        failure_category="lint",
        status="QUEUED",
        attempt=1,
    )
    session.add(run)
    await session.commit()
    log.info("sim.fix_run_created", run_id=run_id[:8], commit=commit_sha[:12], branch=meta["branch"])
    return run_id


async def run_simulation(log_file: Path, dry_run: bool) -> bool:
    meta = _load_meta()
    raw_log = _load_log(log_file)
    commit_sha = meta["commit_sha"]

    log.info(
        "sim.config",
        repo=meta["repo"],
        branch=meta["branch"],
        commit=commit_sha[:12],
        pipeline=meta["pipeline_number"],
        log_chars=len(raw_log),
        dry_run=dry_run,
    )

    from phalanx.db.session import get_db

    async with get_db() as session:
        integration_id = await _setup_integration(session, meta["repo"])

    async with get_db() as session:
        run_id = await _create_fix_run(session, integration_id, meta, commit_sha)

    log.info("sim.starting_agent", run_id=run_id[:8])

    # ── Only mock: log fetcher reads from disk instead of hitting CircleCI API ──
    # Process through the same ANSI-strip + extract pipeline the real fetcher uses,
    # so the parser sees exactly what it would see from a live CircleCI fetch.
    import re as _re
    from phalanx.ci_fixer.log_fetcher import _clean_log_lines, _extract_failure_section

    _ansi = _re.compile(r"\x1b\[[0-9;]*[mGKHF]")
    clean_lines = _clean_log_lines(_ansi.sub("", raw_log).splitlines())
    processed_log = _extract_failure_section(clean_lines)
    log.info("sim.log_processed", raw_chars=len(raw_log), processed_chars=len(processed_log))

    mock_fetcher = MagicMock()
    mock_fetcher.fetch = AsyncMock(return_value=processed_log)

    patch_targets = [
        patch("phalanx.agents.ci_fixer.get_log_fetcher", return_value=mock_fetcher),
    ]

    if dry_run:
        from phalanx.ci_fixer.context import VerificationResult

        patch_targets += [
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


def main():
    parser = argparse.ArgumentParser(description="CI Fixer prod-parity simulation")
    parser.add_argument("--dry-run", action="store_true", help="Skip git push and PR (still clones, sandboxes, LLM, validates)")
    parser.add_argument("--fetch-logs", action="store_true", help="Re-fetch logs from CircleCI before running")
    parser.add_argument("--log-file", default=None, help="Override log file path (default: sim_logs/combined_failure_log.txt)")
    parser.add_argument("--repo", default="MESMD/mesmd-ai-bht-multi-agent-platform-app")
    parser.add_argument("--branch", default="feat/observability-issues")
    args = parser.parse_args()

    import os
    os.environ.setdefault("FORGE_WORKER", "1")

    async def _main():
        if args.fetch_logs:
            log.info("sim.fetching_logs", repo=args.repo, branch=args.branch)
            await _fetch_logs_from_circleci(args.repo, args.branch)

        log_file = Path(args.log_file) if args.log_file else COMBINED_LOG_FILE

        success = await run_simulation(log_file=log_file, dry_run=args.dry_run)
        print(f"\n{'✅ SIMULATION PASSED' if success else '❌ SIMULATION FAILED'}")
        sys.exit(0 if success else 1)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
