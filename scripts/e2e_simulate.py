"""
FORGE E2E Simulation — No Slack, No Celery workers needed.

Runs the full pipeline in-process by calling agents as Python objects:
  WorkOrder → Commander planning → Plan auto-approved → Planner → Builder → Reviewer → QA → Security → Release

Reports each step with DB evidence.

Usage:
    FORGE_WORKER=1 python scripts/e2e_simulate.py
    FORGE_WORKER=1 python scripts/e2e_simulate.py --title "My feature" --prompt "Build X that does Y"
    FORGE_WORKER=1 python scripts/e2e_simulate.py --prompt-file scripts/prompts/my_prompt.txt
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Saved prompts (reusable) ──────────────────────────────────────────────────
PROMPTS: dict[str, dict] = {
    "healthcheck": {
        "title": "Add health-check endpoint",
        "description": (
            "Add a GET /health endpoint that returns {status: ok, version: <current>}. "
            "It should check DB connectivity and return 503 if unhealthy. "
            "Add tests. Follow existing FastAPI patterns."
        ),
        "command": "/phalanx build Add health-check endpoint",
    },
    "nextjs-demo": {
        "title": "Build polished Next.js demo site for Phalanx",
        "description": (
            "build a polished nextjs demo site for phalanx\n\n"
            "brand: Phalanx\n"
            "tagline: from slack command to shipped software\n\n"
            "tech:\n"
            "- next.js 14 app router\n"
            "- typescript\n"
            "- tailwind css\n\n"
            "sections:\n"
            "1. hero with tagline and CTA\n"
            "2. how it works (slack command → plan → code → shipped)\n"
            "3. feature grid\n"
            "4. pricing cards\n"
            "5. faq accordion\n"
            "6. final call to action\n\n"
            "design:\n"
            "- modern startup homepage look\n"
            "- clean typography\n"
            "- subtle gradients ok\n"
            "- credible, not flashy\n"
            "- responsive (mobile + desktop)\n\n"
            "constraints:\n"
            "- one page only\n"
            "- no backend, no login, no database\n"
            "- use mock data throughout\n"
            "- no paid external dependencies"
        ),
        "command": "/phalanx build polished nextjs demo site for phalanx",
    },
}

# ── ANSI ──────────────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"{GREEN}  ✓ {msg}{RESET}")
def err(msg):  print(f"{RED}  ✗ {msg}{RESET}")
def info(msg): print(f"{CYAN}  → {msg}{RESET}")
def hdr(msg):  print(f"\n{BOLD}{msg}{RESET}")
def sep():     print(f"{YELLOW}{'─'*60}{RESET}")


# ── DB state printer ──────────────────────────────────────────────────────────

async def get_db_state(session, run_id: str) -> dict:
    from sqlalchemy import select
    from phalanx.db.models import Run, Task, Approval

    run_res = await session.execute(select(Run).where(Run.id == run_id))
    run = run_res.scalar_one_or_none()

    tasks_res = await session.execute(
        select(Task).where(Task.run_id == run_id).order_by(Task.sequence_num)
    )
    tasks = list(tasks_res.scalars())

    approvals_res = await session.execute(
        select(Approval).where(Approval.run_id == run_id)
    )
    approvals = list(approvals_res.scalars())

    return {"run": run, "tasks": tasks, "approvals": approvals}


def print_db_state(state: dict, label: str) -> None:
    sep()
    print(f"{BOLD}DB STATE — {label}{RESET}")
    run = state["run"]
    if run:
        status_color = GREEN if run.status in ("COMPLETED", "READY_TO_MERGE") else (RED if run.status == "FAILED" else CYAN)
        print(f"  Run  id={run.id[:8]}…  status={status_color}{BOLD}{run.status}{RESET}  branch={run.active_branch}")
    for t in state["tasks"]:
        if t.status == "COMPLETED":
            icon, color = "✓", GREEN
        elif t.status in ("FAILED", "CANCELLED"):
            icon, color = "✗", RED
        elif t.status == "IN_PROGRESS":
            icon, color = "⟳", CYAN
        else:
            icon, color = "·", YELLOW
        tokens = f"  tokens={t.actual_complexity}" if t.actual_complexity else ""
        print(f"  [{color}{icon}{RESET}] seq={t.sequence_num:02d}  {t.agent_role:<12}  {color}{t.status:<14}{RESET}  {t.title[:50]}{tokens}")
    for ap in state["approvals"]:
        color = GREEN if ap.status == "APPROVED" else (RED if ap.status == "REJECTED" else YELLOW)
        print(f"  [{color}⊙{RESET}] gate={ap.gate_type}  status={color}{ap.status}{RESET}  by={ap.decided_by}")
    sep()


# ── Main simulation ───────────────────────────────────────────────────────────

async def run_simulation(prompt_key: str = "healthcheck", title: str | None = None, description: str | None = None) -> None:
    from sqlalchemy import select, update
    from phalanx.db.session import get_db
    from phalanx.db.models import Project, Channel, WorkOrder, Run, Task, Approval
    from phalanx.agents.commander import CommanderAgent
    from phalanx.config.settings import get_settings
    from phalanx.config.loader import ConfigLoader
    from phalanx.memory.reader import MemoryReader
    from phalanx.memory.assembler import MemoryAssembler

    hdr("╔══════════════════════════════════════════════════════════╗")
    hdr("║  FORGE E2E SIMULATION — Full Pipeline Test               ║")
    hdr("╚══════════════════════════════════════════════════════════╝")

    settings = get_settings()
    info(f"env={settings.forge_env}  db=localhost:5433/forge")
    info(f"model={settings.anthropic_model_default}")

    # ── Step 1: Seed project + channel ───────────────────────────
    hdr("STEP 1 — Seed Project & Channel")
    async with get_db() as session:
        result = await session.execute(
            select(Project).where(Project.slug == "sim-project")
        )
        project = result.scalar_one_or_none()
        if project is None:
            project = Project(
                slug="sim-project",
                name="Simulation Project",
                repo_url="https://github.com/usephalanx/phalanx",
                domain="backend",
                config={"stack": {"language": "python", "framework": "fastapi"}},
                onboarding_status="complete",
            )
            session.add(project)
            await session.flush()
            ok(f"Created project  id={project.id[:8]}…")
        else:
            ok(f"Project exists   id={project.id[:8]}…")
        project_id = project.id

        result = await session.execute(
            select(Channel).where(
                Channel.project_id == project_id,
                Channel.platform == "slack",
            )
        )
        channel = result.scalar_one_or_none()
        if channel is None:
            channel = Channel(
                project_id=project_id,
                platform="slack",
                channel_id="C_SIM_TEST",
                display_name="#sim-test",
            )
            session.add(channel)
            await session.flush()
            ok(f"Created channel  id={channel.id[:8]}…")
        else:
            ok(f"Channel exists   id={channel.id[:8]}…")
        channel_id = channel.id

    # ── Step 2: Create WorkOrder ──────────────────────────────────
    hdr("STEP 2 — Create WorkOrder")
    _prompt = PROMPTS.get(prompt_key, PROMPTS["healthcheck"])
    _title = title or _prompt["title"]
    _description = description or _prompt["description"]
    _command = _prompt.get("command", f"/phalanx build {_title}")
    async with get_db() as session:
        work_order = WorkOrder(
            project_id=project_id,
            channel_id=channel_id,
            title=_title,
            description=_description,
            raw_command=_command,
            status="OPEN",
            priority=75,
            requested_by="U_SIM_001",
        )
        session.add(work_order)
        await session.flush()
        work_order_id = work_order.id
        ok(f"WorkOrder  id={work_order_id[:8]}…  title={work_order.title!r}")

    # ── Step 3: Commander planning phase (no blocking wait) ───────
    hdr("STEP 3 — Commander: Decompose WorkOrder into Tasks")
    run_id = str(uuid.uuid4())

    commander = CommanderAgent(
        run_id=run_id,
        work_order_id=work_order_id,
        project_id=project_id,
        agent_id="commander-sim",
    )

    # Call only the planning internals (not full execute which blocks on approval gate)
    async with get_db() as session:
        wo = await session.get(WorkOrder, work_order_id)

        # Create Run
        from phalanx.db.models import Run
        from sqlalchemy import func, select
        count_result = await session.execute(
            select(func.count()).select_from(Run).where(Run.work_order_id == wo.id)
        )
        existing_count = count_result.scalar_one()
        run = Run(
            id=run_id,
            work_order_id=wo.id,
            project_id=project_id,
            run_number=existing_count + 1,
            status="INTAKE",
        )
        session.add(run)
        await session.commit()
        ok(f"Run created  id={run_id[:8]}…  status=INTAKE")

        # Transition INTAKE → RESEARCHING → PLANNING
        await session.execute(
            update(Run).where(Run.id == run_id)
            .values(status="RESEARCHING", updated_at=datetime.now(UTC))
        )
        await session.commit()

        loader = ConfigLoader()
        reader = MemoryReader(session, project_id)
        standing_facts = await reader.get_standing_facts()
        decisions = await reader.get_standing_decisions()
        assembler = MemoryAssembler(max_tokens=4000)
        memory_block = assembler.build(decisions=decisions, standing_facts=standing_facts)

        await session.execute(
            update(Run).where(Run.id == run_id)
            .values(status="PLANNING", updated_at=datetime.now(UTC))
        )
        await session.commit()

    info("Calling Claude to decompose WorkOrder → Tasks…")
    task_plan = await commander._generate_task_plan(wo, memory_block)

    tasks_in_plan = task_plan.get("tasks", [])
    info(f"Claude returned {len(tasks_in_plan)} tasks")
    for t in tasks_in_plan:
        print(f"      seq={t.get('sequence_num', '?'):02}  {t.get('agent_role', '?'):<12}  {t.get('title', '')[:50]}")

    # Persist tasks to DB
    async with get_db() as session:
        await commander._persist_task_plan(session, task_plan)
        await session.commit()

        # Transition → AWAITING_PLAN_APPROVAL
        await session.execute(
            update(Run).where(Run.id == run_id)
            .values(status="AWAITING_PLAN_APPROVAL", updated_at=datetime.now(UTC))
        )
        await session.commit()

        # Create approval gate record
        approval = Approval(
            run_id=run_id,
            gate_type="plan",
            gate_phase="planning",
            status="PENDING",
            context_snapshot={"plan": task_plan},
        )
        session.add(approval)
        await session.commit()
        approval_id = approval.id

    ok(f"Tasks persisted  count={len(tasks_in_plan)}  approval=PENDING")

    async with get_db() as session:
        state = await get_db_state(session, run_id)
    print_db_state(state, "After Commander Planning")

    # ── Step 4: Auto-approve plan gate ────────────────────────────
    hdr("STEP 4 — Auto-Approve Plan Gate")
    async with get_db() as session:
        await session.execute(
            update(Approval).where(Approval.id == approval_id)
            .values(
                status="APPROVED",
                decided_by="U_SIM_AUTO",
                decided_at=datetime.now(UTC),
            )
        )
        await session.execute(
            update(Run).where(Run.id == run_id)
            .values(status="EXECUTING", updated_at=datetime.now(UTC))
        )
        await session.commit()
    ok("Plan approved (auto)  →  Run status = EXECUTING")

    async with get_db() as session:
        state = await get_db_state(session, run_id)
    tasks = state["tasks"]

    # ── Step 5–N: Run each agent in sequence ──────────────────────
    import importlib
    from phalanx.config.settings import get_settings as _get_settings
    _sim_settings = _get_settings()

    # QA needs the workspace path where builder wrote files
    async with get_db() as session:
        _run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
    workspace_path = Path(_sim_settings.git_workspace) / _run.project_id / run_id

    AGENT_MAP = {
        "planner":  ("phalanx.agents.planner",  "PlannerAgent",  "execute",  {}),
        "builder":  ("phalanx.agents.builder",  "BuilderAgent",  "execute",  {}),
        "reviewer": ("phalanx.agents.reviewer", "ReviewerAgent", "execute",  {}),
        # QA runs pytest in the generated workspace; use absolute venv pytest + --cov=.
        "qa":       ("phalanx.agents.qa",       "QAAgent",       "evaluate",
                     {"repo_path": workspace_path,
                      "test_command": [str(Path(sys.executable).parent / "pytest"),
                                       "--tb=short", "-q",
                                       "--junit-xml=test-results.xml",
                                       "--cov=.", "--cov-report=xml:coverage.xml"]}),
        "security": ("phalanx.agents.security", "SecurityAgent", "execute",  {}),
        "release":  ("phalanx.agents.release",  "ReleaseAgent",  "execute",  {}),
    }

    step = 5
    for task in sorted(tasks, key=lambda t: t.sequence_num):
        role = task.agent_role
        hdr(f"STEP {step} — {role.upper()} Agent  seq={task.sequence_num}  task_id={task.id[:8]}…")
        step += 1

        if role not in AGENT_MAP:
            info(f"Skipping unrecognised role: {role}")
            continue

        module_name, class_name, method_name, extra_kwargs = AGENT_MAP[role]

        mod = importlib.import_module(module_name)
        AgentClass = getattr(mod, class_name)

        # Mark IN_PROGRESS
        async with get_db() as session:
            await session.execute(
                update(Task).where(Task.id == task.id)
                .values(status="IN_PROGRESS", started_at=datetime.now(UTC))
            )
            await session.commit()

        info(f"Running {class_name}.{method_name}()…")

        # Build constructor kwargs based on agent type
        if role == "qa":
            agent_instance = AgentClass(
                run_id=run_id,
                task_id=task.id,
                **extra_kwargs,
            )
        else:
            agent_instance = AgentClass(
                run_id=run_id,
                task_id=task.id,
                agent_id=f"{role}-sim",
            )

        try:
            agent_method = getattr(agent_instance, method_name)
            agent_result = await agent_method()

            # Normalize result — QA returns QAReport, others return AgentResult
            if hasattr(agent_result, "success"):
                success = agent_result.success
                tokens = agent_result.tokens_used
                error_msg = agent_result.error
            elif hasattr(agent_result, "outcome"):
                # QAReport: outcome is QAOutcome enum
                from phalanx.agents.qa import QAOutcome
                success = agent_result.outcome == QAOutcome.PASSED
                tokens = 0
                error_msg = None if success else str(agent_result.outcome)
            else:
                success = True
                tokens = 0
                error_msg = None

            if success:
                ok(f"{role} COMPLETED  tokens={tokens}")
                # For QA, mark task COMPLETED ourselves (Celery task normally does this)
                if role == "qa":
                    async with get_db() as session:
                        await session.execute(
                            update(Task).where(Task.id == task.id)
                            .values(
                                status="COMPLETED",
                                output={"outcome": str(agent_result.outcome)},
                                completed_at=datetime.now(UTC),
                            )
                        )
                        await session.commit()
            else:
                err(f"{role} FAILED: {error_msg}")
                async with get_db() as session:
                    await session.execute(
                        update(Task).where(Task.id == task.id)
                        .values(status="FAILED", error=str(error_msg)[:500], completed_at=datetime.now(UTC))
                    )
                    await session.commit()
                # Don't break — continue with remaining tasks to see full picture
        except Exception as exc:
            import traceback
            err(f"{role} raised exception: {exc}")
            traceback.print_exc()
            async with get_db() as session:
                await session.execute(
                    update(Task).where(Task.id == task.id)
                    .values(status="FAILED", error=str(exc)[:500], completed_at=datetime.now(UTC))
                )
                await session.commit()

        # Auto-approve ship gate if it appears
        async with get_db() as session:
            result_ap = await session.execute(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.gate_type == "ship",
                    Approval.status == "PENDING",
                )
            )
            ship_approval = result_ap.scalar_one_or_none()
            if ship_approval:
                await session.execute(
                    update(Approval).where(Approval.id == ship_approval.id)
                    .values(
                        status="APPROVED",
                        decided_by="U_SIM_AUTO",
                        decided_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                ok("Ship gate approved (auto)")

        # Refresh task list from DB (agents may have created additional tasks)
        async with get_db() as session:
            state = await get_db_state(session, run_id)
        tasks = state["tasks"]  # refresh

        print_db_state(state, f"After {role}")

    # ── Final transition ──────────────────────────────────────────
    async with get_db() as session:
        state = await get_db_state(session, run_id)
        all_tasks = state["tasks"]
        completed = [t for t in all_tasks if t.status == "COMPLETED"]
        failed    = [t for t in all_tasks if t.status == "FAILED"]
        pending   = [t for t in all_tasks if t.status in ("PENDING", "IN_PROGRESS")]

        final_status = "READY_TO_MERGE" if (len(failed) == 0 and len(pending) == 0) else "FAILED"
        await session.execute(
            update(Run).where(Run.id == run_id)
            .values(status=final_status, updated_at=datetime.now(UTC))
        )
        await session.commit()

    # ── Final report ──────────────────────────────────────────────
    hdr("══════════════════════ FINAL REPORT ════════════════════════")
    async with get_db() as session:
        state = await get_db_state(session, run_id)
    print_db_state(state, "SIMULATION COMPLETE")

    all_tasks = state["tasks"]
    completed = [t for t in all_tasks if t.status == "COMPLETED"]
    failed    = [t for t in all_tasks if t.status == "FAILED"]
    pending   = [t for t in all_tasks if t.status in ("PENDING", "IN_PROGRESS")]

    print()
    print(f"  Total tasks:     {len(all_tasks)}")
    print(f"  {GREEN}Completed:       {len(completed)}{RESET}")
    print(f"  {RED}Failed:          {len(failed)}{RESET}")
    print(f"  {YELLOW}Pending/Active:  {len(pending)}{RESET}")
    print(f"  Run status:      {BOLD}{state['run'].status if state['run'] else 'N/A'}{RESET}")
    print(f"  Run ID:          {run_id}")
    print()

    if len(failed) == 0 and len(pending) == 0:
        print(f"{GREEN}{BOLD}  ✅ ALL TASKS COMPLETED — Pipeline is solid!{RESET}")
    elif len(failed) > 0:
        print(f"{RED}{BOLD}  ❌ {len(failed)} task(s) failed — see errors above{RESET}")
    else:
        print(f"{YELLOW}{BOLD}  ⚠  {len(pending)} task(s) still pending{RESET}")
    print()

    # ── Push generated workspace to usephalanx/showcase ───────────────────────
    await _push_to_showcase(workspace_path, run_id, prompt_key, len(completed), len(failed))


async def _push_to_showcase(
    workspace_path: Path,
    run_id: str,
    prompt_key: str,
    completed: int,
    failed: int,
) -> None:
    """
    Clone usephalanx/showcase, copy the generated workspace into
    showcase/<prompt_key>/<short_run_id>/, commit, and push.

    Requires GITHUB_TOKEN env var with write access to usephalanx/showcase.
    Skips silently if workspace is empty or token is missing.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("PHALANX_GITHUB_TOKEN")
    if not token:
        print(f"  {YELLOW}showcase: skipped — GITHUB_TOKEN not set{RESET}")
        return

    if not workspace_path.exists() or not any(workspace_path.iterdir()):
        print(f"  {YELLOW}showcase: skipped — workspace empty{RESET}")
        return

    showcase_url = f"https://{token}@github.com/usephalanx/showcase.git"
    short_id = run_id[:8]
    dest_name = f"{prompt_key}/{short_id}"

    print(f"\n  {CYAN}→ Pushing to usephalanx/showcase/{dest_name}...{RESET}")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", showcase_url, tmpdir],
                check=True, capture_output=True,
            )

            dest = Path(tmpdir) / prompt_key / short_id
            dest.mkdir(parents=True, exist_ok=True)

            # Copy workspace files, skip git internals and node_modules
            for item in workspace_path.iterdir():
                if item.name in (".git", "node_modules", "__pycache__", ".next"):
                    continue
                dst = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, ignore=shutil.ignore_patterns(
                        "node_modules", "__pycache__", ".next", "*.pyc"
                    ))
                else:
                    shutil.copy2(item, dst)

            # Write a run summary
            summary = dest / "_phalanx_run.md"
            summary.write_text(
                f"# Phalanx Run — {prompt_key}/{short_id}\n\n"
                f"- **Run ID**: `{run_id}`\n"
                f"- **Prompt**: `{prompt_key}`\n"
                f"- **Tasks completed**: {completed}\n"
                f"- **Tasks failed**: {failed}\n"
                f"- **Generated**: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"_Built end-to-end by Phalanx agents from a single command._\n"
            )

            env = {**os.environ,
                   "GIT_AUTHOR_NAME": "Raj Nagulapalle",
                   "GIT_AUTHOR_EMAIL": "raj.nagulapalle@kraken.com",
                   "GIT_COMMITTER_NAME": "Raj Nagulapalle",
                   "GIT_COMMITTER_EMAIL": "raj.nagulapalle@kraken.com"}

            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True, capture_output=True, env=env)
            subprocess.run(
                ["git", "-C", tmpdir, "commit", "-m",
                 f"add: {prompt_key}/{short_id} — {completed} tasks, {failed} failed"],
                check=True, capture_output=True, env=env,
            )
            subprocess.run(
                ["git", "-C", tmpdir, "push", "origin", "main"],
                check=True, capture_output=True, env=env,
            )
            print(f"  {GREEN}✓ showcase: pushed to usephalanx/showcase/{dest_name}{RESET}")

        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode(errors="replace").strip()
            print(f"  {YELLOW}showcase: push failed — {err[:120]}{RESET}")
        except Exception as exc:
            print(f"  {YELLOW}showcase: push failed — {exc}{RESET}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FORGE E2E Simulation")
    parser.add_argument("--prompt", choices=list(PROMPTS.keys()), default="healthcheck",
                        help=f"Saved prompt to use. Options: {list(PROMPTS.keys())}")
    parser.add_argument("--title", default=None, help="Override the work order title")
    parser.add_argument("--description", default=None, help="Override the work order description")
    parser.add_argument("--prompt-file", default=None, help="Load description from a text file")
    args = parser.parse_args()

    description = args.description
    if args.prompt_file:
        description = Path(args.prompt_file).read_text().strip()

    asyncio.run(run_simulation(
        prompt_key=args.prompt,
        title=args.title,
        description=description,
    ))
