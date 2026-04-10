"""
prod_simulate.py — FORGE Production E2E Simulator

Triggers a real run through prod infrastructure end-to-end:
  1. Creates a WorkOrder via REST API
  2. Dispatches Commander to Celery queue (real worker picks it up)
  3. Polls DB for approval gates → auto-approves in DB
  4. Polls DB for task status, posts live updates to Slack
  5. Reports final result + any breaking stage

Usage (on prod server):
    cd /app
    python scripts/prod_simulate.py --title "Simple Todo Webapp" \
        --description "Build a todo app with FastAPI + SQLite + vanilla JS"

    python scripts/prod_simulate.py --title "Hello World API" \
        --description "Build a GET /hello endpoint that returns {message: hello world}"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

import structlog

# ── Allow running from /app in Docker ────────────────────────────────────────
sys.path.insert(0, "/app")

log = structlog.get_logger("prod_simulate")

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL = 15       # seconds between status polls
MAX_WAIT_MINUTES = 60    # bail if run doesn't complete in 60 min
PHALANX_SHOWCASE_PROJECT_ID = "bb269cba-4eb2-4ab1-8558-e4f174c396fe"
SLACK_CHANNEL = "C0AJ3DCUS"   # #phalanx-showcase channel

# ── Terminal colours ──────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def info(msg: str)  -> None: print(f"  {msg}")
def ok(msg: str)    -> None: print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg: str)  -> None: print(f"  {RED}✗{RESET} {msg}")
def step(msg: str)  -> None: print(f"\n{BOLD}{msg}{RESET}")
def sep()           -> None: print(f"{YELLOW}{'─'*68}{RESET}")


# ── Slack posting helper ──────────────────────────────────────────────────────

async def slack_post(client, channel: str, text: str, blocks=None) -> str | None:
    """Post to Slack, return ts (thread timestamp) or None on error."""
    try:
        kw = {"channel": channel, "text": text}
        if blocks:
            kw["blocks"] = blocks
        resp = await client.chat_postMessage(**kw)
        return resp["ts"]
    except Exception as exc:
        log.warning("slack_post_failed", error=str(exc))
        return None


async def slack_update(client, channel: str, ts: str, text: str, blocks=None) -> None:
    """Update an existing Slack message."""
    try:
        kw = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            kw["blocks"] = blocks
        await client.chat_update(**kw)
    except Exception as exc:
        log.warning("slack_update_failed", error=str(exc))


# ── Status emoji helpers ──────────────────────────────────────────────────────

def task_emoji(status: str) -> str:
    return {"COMPLETED": "✅", "FAILED": "❌", "IN_PROGRESS": "⏳", "PENDING": "⬜"}.get(status, "❓")


def build_status_blocks(run_id: str, run_status: str, tasks: list, start_ts: datetime) -> list:
    elapsed = int((datetime.now(UTC) - start_ts).total_seconds())
    mins, secs = divmod(elapsed, 60)

    lines = [f"{task_emoji(t['status'])} `seq={t['seq']:02}` *{t['role']:<10}* {t['status']:<12}  _{t['title'][:50]}_"
             for t in tasks]

    run_icon = {"EXECUTING": "🔄", "FAILED": "💥", "READY_TO_MERGE": "🚀",
                "PLANNING": "🧠", "AWAITING_PLAN_APPROVAL": "⏸️"}.get(run_status, "❓")

    header = f"{run_icon} *FORGE Run* `{run_id[:8]}…`  |  Status: *{run_status}*  |  Elapsed: {mins}m {secs}s"

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) if lines else "_No tasks yet_"}},
    ]


# ── Core simulation ───────────────────────────────────────────────────────────

async def run_simulation(title: str, description: str) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    from phalanx.config.settings import get_settings
    from phalanx.db.models import Approval, Run, Task, TaskDependency, WorkOrder
    from phalanx.db.session import get_db
    from phalanx.queue.celery_app import celery_app
    from phalanx.runtime.task_router import TaskRouter
    from sqlalchemy import select, update

    settings = get_settings()
    slack = AsyncWebClient(token=settings.slack_bot_token)

    start_ts = datetime.now(UTC)
    sep()
    print(f"{BOLD}{'═'*68}{RESET}")
    print(f"{BOLD}  PHALANX — PROD SIMULATION{RESET}")
    print(f"{BOLD}{'═'*68}{RESET}")
    info(f"Title:       {title}")
    info(f"Description: {description[:80]}…" if len(description) > 80 else f"Description: {description}")
    info(f"Target:      prod DB + prod Celery worker")
    info(f"Slack:       #{SLACK_CHANNEL}")
    sep()

    # ── STEP 1: Create WorkOrder ──────────────────────────────────────────────
    step("STEP 1 — Create WorkOrder + Dispatch to Commander")

    wo_id: str
    run_id: str

    async with get_db() as session:
        wo = WorkOrder(
            project_id=PHALANX_SHOWCASE_PROJECT_ID,
            title=title,
            description=description,
            raw_command=f"/phalanx build {title}",
            requested_by="prod-simulator",
            priority=75,
            status="OPEN",
        )
        session.add(wo)
        await session.commit()
        await session.refresh(wo)
        wo_id = str(wo.id)

    ok(f"WorkOrder created  id={wo_id[:8]}…")

    # Dispatch Commander to real Celery worker
    router = TaskRouter(celery_app)
    router.dispatch(
        agent_role="commander",
        task_id=wo_id,
        run_id=wo_id,
        payload={"work_order_id": wo_id, "project_id": PHALANX_SHOWCASE_PROJECT_ID},
    )
    ok(f"Commander dispatched to Celery queue")

    # ── STEP 2: Wait for Run to be created by Commander ──────────────────────
    step("STEP 2 — Waiting for Commander to create Run…")

    run_id = None
    for _ in range(20):  # up to 100s
        await asyncio.sleep(5)
        async with get_db() as session:
            result = await session.execute(
                select(Run).where(Run.work_order_id == wo_id).order_by(Run.created_at.desc()).limit(1)
            )
            run = result.scalar_one_or_none()
            if run:
                run_id = str(run.id)
                break

    if not run_id:
        fail("Commander never created a Run — check worker logs")
        await slack_post(slack, SLACK_CHANNEL,
            f"💥 *FORGE Simulation FAILED*\nCommander never created a Run for WorkOrder `{wo_id[:8]}`")
        return

    ok(f"Run created  id={run_id[:8]}…")

    # Post initial Slack message
    slack_ts = await slack_post(
        slack, SLACK_CHANNEL,
        f"🧠 *FORGE Simulation Started*\n*{title}*\nRun: `{run_id[:8]}…`\nWaiting for plan…",
    )

    # ── STEP 3: Poll loop — approve gates, watch tasks, report progress ───────
    step("STEP 3 — Monitoring pipeline (auto-approving gates)")

    max_polls = (MAX_WAIT_MINUTES * 60) // POLL_INTERVAL
    last_task_states: dict[str, str] = {}
    plan_approved = False
    breaking_stage: str | None = None

    for poll_num in range(max_polls):
        await asyncio.sleep(POLL_INTERVAL)

        async with get_db() as session:
            # Fetch run
            run_result = await session.execute(select(Run).where(Run.id == run_id))
            run = run_result.scalar_one_or_none()
            if not run:
                continue

            # Fetch tasks
            task_result = await session.execute(
                select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num)
            )
            tasks = task_result.scalars().all()

            # Fetch pending approvals
            approval_result = await session.execute(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.status == "PENDING",
                )
            )
            pending_approvals = approval_result.scalars().all()

            # Auto-approve any pending gates
            for approval in pending_approvals:
                gate = approval.gate_type
                await session.execute(
                    update(Approval)
                    .where(Approval.id == approval.id)
                    .values(
                        status="APPROVED",
                        decided_by="prod-simulator",
                        decided_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                ok(f"Auto-approved gate: {gate}  (approval_id={str(approval.id)[:8]}…)")

                if not plan_approved and gate == "plan":
                    plan_approved = True
                    await slack_post(
                        slack, SLACK_CHANNEL,
                        f"✅ *Plan approved* — {len(tasks)} tasks queued\n"
                        + "\n".join(f"  `{t.sequence_num:02}` {t.agent_role} — {t.title[:60]}" for t in tasks),
                    )

        # Print + update Slack with current state
        task_rows = [{"seq": t.sequence_num, "role": t.agent_role,
                      "status": t.status, "title": t.title} for t in tasks]
        run_status = run.status

        elapsed = int((datetime.now(UTC) - start_ts).total_seconds())
        mins, secs = divmod(elapsed, 60)

        # Detect newly completed/failed tasks since last poll
        for t in tasks:
            prev = last_task_states.get(str(t.id))
            if prev != t.status:
                if t.status == "COMPLETED":
                    ok(f"[{mins}m{secs:02}s] seq={t.sequence_num:02} {t.agent_role:<10} COMPLETED  {t.title[:55]}")
                    await slack_post(
                        slack, SLACK_CHANNEL,
                        f"✅ `seq={t.sequence_num:02}` *{t.agent_role}* COMPLETED — _{t.title[:60]}_",
                    )
                elif t.status == "FAILED":
                    fail(f"[{mins}m{secs:02}s] seq={t.sequence_num:02} {t.agent_role:<10} FAILED     {t.title[:55]}")
                    if t.agent_role not in ("qa", "security") and breaking_stage is None:
                        breaking_stage = f"seq={t.sequence_num:02} {t.agent_role}: {t.error or 'unknown'}"
                    await slack_post(
                        slack, SLACK_CHANNEL,
                        f"{'⚠️' if t.agent_role in ('qa','security') else '❌'} `seq={t.sequence_num:02}` *{t.agent_role}* FAILED — _{t.title[:60]}_"
                        + (f"\n```{t.error[:300]}```" if t.error else ""),
                    )
                elif t.status == "IN_PROGRESS" and prev == "PENDING":
                    info(f"[{mins}m{secs:02}s] seq={t.sequence_num:02} {t.agent_role:<10} IN_PROGRESS")
            last_task_states[str(t.id)] = t.status

        # Update Slack status board
        if slack_ts:
            await slack_update(
                slack, SLACK_CHANNEL, slack_ts,
                f"🔄 *FORGE Simulation* `{run_id[:8]}…` | {run_status} | {mins}m{secs:02}s",
                blocks=build_status_blocks(run_id, run_status, task_rows, start_ts),
            )

        # Terminal conditions
        if run_status in ("READY_TO_MERGE", "SHIPPED", "AWAITING_SHIP_APPROVAL"):
            break
        # Only exit on FAILED if all tasks are in a terminal state (no more progress possible)
        if run_status == "FAILED":
            non_terminal = [t for t in tasks if t.status in ("PENDING", "IN_PROGRESS")]
            if not non_terminal:
                break
            # else keep watching — non-fatal failures set run=FAILED but pipeline continues

    # ── FINAL REPORT ─────────────────────────────────────────────────────────
    sep()
    step("FINAL REPORT")
    sep()

    async with get_db() as session:
        run_result = await session.execute(select(Run).where(Run.id == run_id))
        run = run_result.scalar_one_or_none()
        task_result = await session.execute(
            select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num)
        )
        tasks = task_result.scalars().all()

    total = len(tasks)
    completed = sum(1 for t in tasks if t.status == "COMPLETED")
    failed    = sum(1 for t in tasks if t.status == "FAILED")
    files_written = sum(
        len((t.output or {}).get("files_written", [])) for t in tasks if t.output
    )
    total_tokens = sum(
        (t.output or {}).get("tokens_used", 0) for t in tasks if t.output
    )
    elapsed_total = int((datetime.now(UTC) - start_ts).total_seconds())
    mins_total, secs_total = divmod(elapsed_total, 60)

    run_status = run.status if run else "UNKNOWN"
    success = run_status in ("READY_TO_MERGE", "SHIPPED", "AWAITING_SHIP_APPROVAL")

    print(f"\n  {'Run ID:':<20} {run_id}")
    print(f"  {'Status:':<20} {run_status}")
    print(f"  {'Tasks:':<20} {completed}/{total} completed, {failed} failed")
    print(f"  {'Files written:':<20} {files_written}")
    print(f"  {'Total elapsed:':<20} {mins_total}m {secs_total}s")
    print()

    for t in tasks:
        icon = task_emoji(t.status)
        fw = len((t.output or {}).get("files_written", []))
        print(f"  {icon} seq={t.sequence_num:02}  {t.agent_role:<12}  {t.status:<14}  {t.title[:50]}"
              + (f"  [{fw} files]" if fw else ""))

    sep()

    # Breaking stage analysis
    if breaking_stage:
        fail(f"Breaking stage: {breaking_stage}")
    elif not success:
        fail("Pipeline ended in FAILED — all non-QA/security tasks may still have completed")
    elif run_status == "AWAITING_SHIP_APPROVAL":
        ok("Pipeline complete — awaiting ship approval ✅")
    else:
        ok("Pipeline reached READY_TO_MERGE — build succeeded!")

    # ── SRE demo URL check ────────────────────────────────────────────────────
    demo_url = run.deploy_url if run else None
    demo_reachable: bool | None = None
    if demo_url:
        import urllib.request
        step("SRE DEMO CHECK")
        try:
            req = urllib.request.Request(demo_url, method="GET")
            req.add_header("User-Agent", "phalanx-sim/1.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                demo_reachable = resp.status < 400
            if demo_reachable:
                ok(f"Demo reachable: {demo_url}  (HTTP {resp.status})")
            else:
                fail(f"Demo returned HTTP {resp.status}: {demo_url}")
        except Exception as exc:
            fail(f"Demo unreachable: {demo_url}  ({exc})")
            demo_reachable = False
        sep()
    else:
        step("SRE DEMO CHECK")
        info("No deploy_url on run — SRE did not complete successfully or demo deploy is disabled")
        sep()

    # Final Slack summary
    status_icon = "🚀" if success else ("⚠️" if completed >= total - 1 else "💥")
    demo_line = ""
    if demo_url:
        demo_line = f"\n*Demo:* {'✅ ' + demo_url if demo_reachable else '❌ unreachable — ' + demo_url}"
    summary_text = (
        f"{status_icon} *FORGE Simulation Complete* — `{title}`\n"
        f"*Status:* {run_status}  |  *Tasks:* {completed}/{total}  |  *Files:* {files_written}  |  *Time:* {mins_total}m {secs_total}s\n"
        + (f"*Breaking stage:* `{breaking_stage}`" if breaking_stage else
           ("✅ All critical stages passed!" if success else "⚠️ Only QA/Security failed (non-fatal)"))
        + demo_line
    )
    await slack_post(slack, SLACK_CHANNEL, summary_text)

    if slack_ts:
        await slack_update(
            slack, SLACK_CHANNEL, slack_ts,
            summary_text,
            blocks=build_status_blocks(run_id, run_status,
                [{"seq": t.sequence_num, "role": t.agent_role,
                  "status": t.status, "title": t.title} for t in tasks],
                start_ts),
        )

    sep()


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FORGE Production E2E Simulator")
    parser.add_argument("--title",       required=True, help="Work order title")
    parser.add_argument("--description", required=True, help="Work order description")
    args = parser.parse_args()

    asyncio.run(run_simulation(title=args.title, description=args.description))
