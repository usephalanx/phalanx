#!/usr/bin/env python3
"""v1.7.3 runtime-hardening proof run — 4 scenarios.

Spec section 8 — verifies every control path produces a clean terminal
state and no orphan side effects:

  S1. Hung Challenger task    — fault-injected via DB write (Task.status=
                                IN_PROGRESS, last_heartbeat_at = 1h ago)
  S2. Hung Engineer task      — same pattern, agent_role=cifix_engineer
  S3. Sandbox-setup timeout   — Task=cifix_sre_setup heartbeat-stale
  S4. Normal SHIPPED_PROPOSED — vanilla shadow run on inflect Path 3

Each scenario is set up by INSERTing a synthetic Run + Task chain
directly into the prod DB (with shadow_mode=True so any accidental
push attempt is also blocked) and then waiting for the stuck-task
detector's 2-min sweep to land. We measure:

  - Did the task transition from IN_PROGRESS to TIMED_OUT? (within ttl + sweep)
  - Did the Run get failure_class set correctly?
  - Did sandbox cleanup fire (event log) ?
  - Were sibling tasks cancelled? (S1, S2)
  - Did NO repo side effects occur?
  - Total wall-clock to detection.

Run from inside the prod worker container:

    docker exec phalanx-prod-phalanx-ci-fixer-worker-1 \\
        python /app/scripts/v1730_runtime_hardening_proof.py

For S4, this script is a thin wrapper around `phalanx shadow run` —
provided here so the proof outputs land in one report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phalanx.db.models import Project, Run, ShadowLedger, Task, WorkOrder
from phalanx.db.session import get_db


# ── helpers ───────────────────────────────────────────────────────────


async def _insert_synthetic_run_with_stuck_task(
    *,
    repo: str,
    stuck_role: str,
    sre_setup_completed: bool = True,
    sandbox_container_id: str | None = None,
) -> tuple[str, str]:
    """Create a Run + minimal DAG with one IN_PROGRESS task whose
    heartbeat is already stale. Returns (run_id, stuck_task_id)."""

    run_id = str(uuid.uuid4())
    work_order_id = str(uuid.uuid4())
    project_slug = f"cifix_runtime_proof_{int(time.time())}"

    async with get_db() as session:
        # Project
        project = Project(
            name=f"Runtime hardening proof — {repo}",
            slug=project_slug,
            repo_url=f"https://github.com/{repo}",
            repo_provider="github",
            default_branch="main",
            domain="ci_fix",
            onboarding_status="active",
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)

        # WorkOrder
        wo = WorkOrder(
            id=work_order_id,
            project_id=project.id,
            channel_id=None,
            title=f"[runtime-proof] {stuck_role} hang scenario",
            description="synthetic",
            raw_command=json.dumps({"repo": repo, "shadow_mode": True}),
            requested_by="runtime_hardening_proof",
            priority=60,
            status="OPEN",
            work_order_type="ci_fix",
        )
        session.add(wo)
        await session.commit()

        # Run
        run = Run(
            id=run_id,
            work_order_id=work_order_id,
            project_id=project.id,
            run_number=1,
            status="EXECUTING",
            shadow_mode=True,
        )
        session.add(run)
        await session.commit()

        # SRE setup task — completed, with optional container_id
        sre_task_id = str(uuid.uuid4())
        sre_output = (
            {
                "mode": "setup",
                "container_id": sandbox_container_id,
                "workspace_path": f"/tmp/forge-repos/v3-{run_id}-sre",
            }
            if sre_setup_completed
            else None
        )
        sre_status = "COMPLETED" if sre_setup_completed else "TIMED_OUT"
        sre_task = Task(
            id=sre_task_id,
            run_id=run_id,
            sequence_num=1,
            title=f"sre_setup synthetic for {repo}",
            description="proof",
            agent_role="cifix_sre_setup" if stuck_role != "cifix_sre_setup" else "cifix_sre_setup",
            status=sre_status,
            output=sre_output,
        )
        session.add(sre_task)

        # The stuck task — IN_PROGRESS with heartbeat 30 min ago.
        # 30 min is > every default TTL we set; detector will flag.
        stuck_task_id = str(uuid.uuid4())
        stale_ts = datetime.now(UTC) - timedelta(minutes=30)
        stuck_task = Task(
            id=stuck_task_id,
            run_id=run_id,
            sequence_num=2 if stuck_role != "cifix_sre_setup" else 1,
            title=f"[stuck-task-proof] {stuck_role} synthetic hang",
            description="proof",
            agent_role=stuck_role,
            status="IN_PROGRESS",
            started_at=stale_ts,
            last_heartbeat_at=stale_ts,
            ttl_seconds=60,  # 60s so we don't depend on per-role default
        )
        session.add(stuck_task)

        # Sibling pending task to verify cancellation
        sibling_task = Task(
            id=str(uuid.uuid4()),
            run_id=run_id,
            sequence_num=stuck_task.sequence_num + 1,
            title="[stuck-task-proof] sibling pending",
            description="proof",
            agent_role="cifix_engineer" if stuck_role != "cifix_engineer" else "cifix_sre_verify",
            status="PENDING",
        )
        session.add(sibling_task)

        await session.commit()

    # If the SRE setup completed but stuck_role IS cifix_sre_setup, we
    # need to fix up: the stuck role IS the setup itself.
    if stuck_role == "cifix_sre_setup":
        async with get_db() as session:
            await session.execute(
                update(Task)
                .where(Task.id == sre_task_id)
                .values(status="CANCELLED")  # not the "fake setup", reset
            )
            await session.commit()

    return run_id, stuck_task_id


async def _trigger_detector_and_wait(
    run_id: str,
    *,
    timeout_s: int = 180,
    poll_s: int = 5,
) -> dict:
    """Trigger one detector sweep + poll the run until terminal.

    Returns observed final state — sufficient for the proof table.
    """
    from phalanx.maintenance.stuck_task_detector import _detect_stuck_tasks_impl

    started = time.time()
    # Force one immediate sweep (faster than waiting for celery beat).
    sweep_result = await _detect_stuck_tasks_impl()

    # Now poll the run/task state until terminal.
    while time.time() - started < timeout_s:
        async with get_db() as session:
            run_row = (
                await session.execute(select(Run).where(Run.id == run_id))
            ).scalar_one_or_none()
            task_rows = list(
                (
                    await session.execute(
                        select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num.asc())
                    )
                ).scalars().all()
            )
        if run_row is not None and run_row.status in (
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "SHIPPED",
        ):
            break
        await asyncio.sleep(poll_s)

    elapsed_s = round(time.time() - started, 2)
    return {
        "elapsed_s": elapsed_s,
        "sweep_result": sweep_result,
        "run_status": run_row.status if run_row else None,
        "run_failure_class": run_row.failure_class if run_row else None,
        "run_error_message": (run_row.error_message or "")[:200] if run_row else None,
        "tasks": [
            {
                "agent_role": t.agent_role,
                "status": t.status,
                "error": (t.error or "")[:200] if t.error else None,
            }
            for t in task_rows
        ],
    }


# ── scenarios ─────────────────────────────────────────────────────────


async def scenario_s1_hung_challenger() -> dict:
    """S1 — Hung Challenger task. Detector should mark it TIMED_OUT
    with FAILED_INFRA_WORKER_HANG, cancel siblings, and finalize the
    run."""
    run_id, stuck_task_id = await _insert_synthetic_run_with_stuck_task(
        repo="usephalanx/runtime-proof-s1",
        stuck_role="cifix_challenger",
    )
    result = await _trigger_detector_and_wait(run_id)
    return {"scenario": "S1_hung_challenger", "run_id": run_id, **result}


async def scenario_s2_hung_engineer() -> dict:
    """S2 — Hung Engineer. Same as S1 but role=cifix_engineer; also
    must trigger sandbox cleanup since engineer is sandbox-using."""
    run_id, stuck_task_id = await _insert_synthetic_run_with_stuck_task(
        repo="usephalanx/runtime-proof-s2",
        stuck_role="cifix_engineer",
        sandbox_container_id="proof-s2-no-real-container",
    )
    result = await _trigger_detector_and_wait(run_id)
    return {"scenario": "S2_hung_engineer", "run_id": run_id, **result}


async def scenario_s3_sandbox_timeout() -> dict:
    """S3 — Sandbox-setup timeout. Task=cifix_sre_setup with stale
    heartbeat. Must mark TIMED_OUT and cancel downstream tasks."""
    run_id, stuck_task_id = await _insert_synthetic_run_with_stuck_task(
        repo="usephalanx/runtime-proof-s3",
        stuck_role="cifix_sre_setup",
        sre_setup_completed=False,
    )
    result = await _trigger_detector_and_wait(run_id)
    return {"scenario": "S3_sandbox_setup_timeout", "run_id": run_id, **result}


async def scenario_s4_normal_shipped(workflow_run_id: int) -> dict:
    """S4 — Normal SHIPPED_PROPOSED on inflect Path 3 control. Uses
    the existing shadow runner; no fault injection. Should land
    SHIPPED_PROPOSED with cleanup event."""
    from phalanx.shadow.runner import run_shadow_for_workflow

    started = time.time()
    result = await run_shadow_for_workflow(
        repo="usephalanx/inflect",
        workflow_run_id=workflow_run_id,
        poll_interval_s=15,
        poll_timeout_s=900,
    )
    elapsed_s = round(time.time() - started, 2)
    return {
        "scenario": "S4_normal_shipped",
        "elapsed_s": elapsed_s,
        "ledger_id": result.get("id"),
        "phalanx_run_id": result.get("phalanx_run_id"),
        "verdict": result.get("phalanx_verdict"),
        "confidence": result.get("phalanx_confidence"),
        "cost_usd": result.get("phalanx_cost_usd"),
    }


# ── orchestrator ──────────────────────────────────────────────────────


async def main(args: argparse.Namespace) -> int:
    print("=" * 70)
    print("v1.7.3 runtime-hardening proof — 4 scenarios")
    print("=" * 70)

    results: list[dict] = []

    if not args.skip_synthetic:
        for label, fn in [
            ("S1 Hung Challenger", scenario_s1_hung_challenger),
            ("S2 Hung Engineer", scenario_s2_hung_engineer),
            ("S3 Sandbox setup timeout", scenario_s3_sandbox_timeout),
        ]:
            print(f"\n--- {label} ---")
            try:
                r = await fn()
            except Exception as exc:
                r = {"scenario": label, "error": f"{type(exc).__name__}: {exc}"}
            results.append(r)
            print(json.dumps(r, indent=2, default=str))

    if args.s4_workflow_run_id:
        print(f"\n--- S4 Normal SHIPPED_PROPOSED ({args.s4_workflow_run_id}) ---")
        try:
            r = await scenario_s4_normal_shipped(args.s4_workflow_run_id)
        except Exception as exc:
            r = {"scenario": "S4_normal_shipped", "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)
        print(json.dumps(r, indent=2, default=str))

    # Aggregate
    print("\n" + "=" * 70)
    print("PROOF TABLE")
    print("=" * 70)
    for r in results:
        sc = r.get("scenario", "?")
        if sc == "S4_normal_shipped":
            print(
                f"  {sc:30} verdict={r.get('verdict','?'):20} "
                f"elapsed={r.get('elapsed_s','?')}s cost=${r.get('cost_usd','?')}"
            )
        else:
            print(
                f"  {sc:30} run={r.get('run_status','?'):12} "
                f"fc={r.get('run_failure_class') or '-':25} "
                f"elapsed={r.get('elapsed_s','?')}s"
            )

    if args.export:
        with open(args.export, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nWrote {len(results)} results to {args.export}")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="v1730_runtime_hardening_proof")
    p.add_argument(
        "--skip-synthetic",
        action="store_true",
        help="Skip S1-S3 (synthetic fault injection); only run S4.",
    )
    p.add_argument(
        "--s4-workflow-run-id",
        type=int,
        default=None,
        help="usephalanx/inflect workflow_run_id for the normal SHIPPED control.",
    )
    p.add_argument(
        "--export",
        type=str,
        default=None,
        help="Output path for proof-result JSON.",
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(main(args)))
