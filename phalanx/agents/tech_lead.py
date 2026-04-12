"""
Tech Lead Agent — architects the implementation plan for a run.

Responsibilities:
  1. Receive the epics from ProductManagerAgent
  2. Design a per-epic task breakdown with agent roles
  3. Identify cross-epic dependencies (DAG edges — full vs artifact)
  4. Write Task rows + TaskDependency rows to the DB
  5. Produce 'api_contract' and 'db_schema' design artifacts (stored as text)
  6. Set estimated_minutes on each task (feeds Orchestrator scheduling)

Output (AgentResult.output):
  {
    "tasks": [
      {
        "id": "<uuid>",
        "epic_id": "<uuid>",
        "title": "...",
        "agent_role": "builder",
        "sequence_num": 1,
        "estimated_minutes": 30,
        "dependencies": [{"depends_on_id": "<uuid>", "dep_type": "full"}]
      },
      ...
    ],
    "api_contract": "OpenAPI YAML string or summary",
    "db_schema": "SQL DDL or table descriptions",
    "critical_path_minutes": 95
  }

Design rules:
  - Uses claude-sonnet (moderate reasoning — needs to think about dependencies)
  - Produces JSON directly from Claude — no markdown parsing
  - Tasks are per-epic: each epic gets 1-3 tasks
  - Dependency types:
      'full'     → downstream waits for upstream COMPLETED
      'artifact' → downstream can start once artifact exists (e.g. API contract)
  - Never writes code; never touches files
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.config.settings import get_settings
from phalanx.db.models import Task, TaskDependency
from phalanx.workflow.dag import DagNode, DagResolver

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a senior tech lead at a software company.
Your job is to create a technical implementation plan from product epics.

Rules:
- Output ONLY valid JSON — no markdown, no prose, no code fences.
- Each epic must have 1-3 tasks. Tasks are assigned to agent roles: builder | reviewer | qa.
- Identify cross-epic dependencies using DAG edges.
  - dep_type "full": downstream waits for upstream task COMPLETED
  - dep_type "artifact": downstream only waits for an artifact (e.g. api_contract) to exist
- estimated_minutes per task: builder 20-60, reviewer 10-20, qa 15-30.
- api_contract: short OpenAPI-style endpoint list (JSON format).
- db_schema: table names + key columns as a short description.
"""

_USER_PROMPT_TEMPLATE = """Create a technical implementation plan for this project:

Title: {title}
App type: {app_type}

Epics (must be covered):
{epics_json}

Return JSON exactly in this shape:
{{
  "api_contract": {{
    "endpoints": [
      {{"method": "GET", "path": "/api/listings", "description": "List all listings"}},
      {{"method": "POST", "path": "/api/listings", "description": "Create listing"}}
    ]
  }},
  "db_schema": {{
    "tables": [
      {{"name": "listings", "columns": ["id", "title", "price", "location", "created_at"]}},
      {{"name": "users", "columns": ["id", "email", "hashed_password", "created_at"]}}
    ]
  }},
  "tasks": [
    {{
      "epic_index": 0,
      "title": "Scaffold project and DB models",
      "agent_role": "builder",
      "sequence_num": 1,
      "estimated_minutes": 30,
      "files_likely_touched": ["models.py", "alembic/versions/"],
      "dependencies": []
    }},
    {{
      "epic_index": 1,
      "title": "Build listings API endpoints",
      "agent_role": "builder",
      "sequence_num": 2,
      "estimated_minutes": 45,
      "files_likely_touched": ["api/listings.py"],
      "dependencies": [
        {{"depends_on_seq": 1, "dep_type": "artifact"}}
      ]
    }}
  ]
}}"""


class TechLeadAgent(BaseAgent):
    """
    Architects the implementation plan for a run.

    Called inline from Commander after ProductManagerAgent, before builders start.
    Creates Task rows and TaskDependency rows (DAG edges).
    """

    AGENT_ROLE = "tech_lead"

    def __init__(self, run_id: str, task_id: str | None = None) -> None:
        super().__init__(run_id=run_id, agent_id="tech_lead", task_id=task_id)
        self._settings = get_settings()

    async def execute(self) -> AgentResult:
        """Not used — TL is called inline via execute_for_run."""
        raise NotImplementedError("Use execute_for_run(session, work_order, pm_output) instead.")

    async def execute_for_run(
        self,
        session: Any,
        work_order: Any,
        pm_output: dict[str, Any],
    ) -> AgentResult:
        """
        Create tasks + dependencies from epics.

        Args:
            session: AsyncSession — for writing Task / TaskDependency rows
            work_order: WorkOrder ORM object
            pm_output: output dict from ProductManagerAgent.execute_for_work_order()

        Returns:
            AgentResult with output dict containing tasks, api_contract, db_schema, critical_path_minutes
        """
        self._log.info("tech_lead.execute.start")

        epics = pm_output.get("epics", [])
        if not epics:
            return AgentResult(
                success=False,
                output={},
                error="TechLead received no epics from ProductManager.",
            )

        app_type = pm_output.get("app_type", "web")
        tech_stack = pm_output.get("tech_stack", "")  # may be empty — auto-detected later
        epics_json = json.dumps(
            [{"index": i, "title": e["title"], "description": e.get("description", "")} for i, e in enumerate(epics)],
            indent=2,
        )

        prompt = _USER_PROMPT_TEMPLATE.format(
            title=work_order.title,
            app_type=app_type,
            epics_json=epics_json,
        )

        messages = [{"role": "user", "content": prompt}]

        try:
            response_text = self._call_claude(
                messages=messages,
                system=_SYSTEM_PROMPT,
                model=self._settings.anthropic_model_default,  # sonnet — needs reasoning
                max_tokens=4096,
            )
        except Exception as exc:
            self._log.error("tech_lead.claude_call_failed", error=str(exc))
            return AgentResult(success=False, output={}, error=str(exc))

        try:
            parsed = _extract_json(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self._log.error("tech_lead.parse_failed", error=str(exc), response=response_text[:200])
            return AgentResult(success=False, output={}, error=f"JSON parse error: {exc}")

        tasks_data: list[dict] = parsed.get("tasks", [])
        if not tasks_data:
            return AgentResult(
                success=False,
                output={},
                error="TechLead produced no tasks.",
            )

        # Build task rows — map sequence_num → task_id for dependency wiring
        seq_to_id: dict[int, str] = {}
        task_rows: list[Task] = []

        for td in tasks_data:
            epic_idx = td.get("epic_index", 0)
            epic = epics[epic_idx] if epic_idx < len(epics) else epics[0]
            task_id = str(uuid.uuid4())
            seq = td.get("sequence_num", len(task_rows) + 1)
            seq_to_id[seq] = task_id

            task = Task(
                id=task_id,
                run_id=str(self.run_id),
                epic_id=epic.get("id"),
                title=td.get("title", "Untitled task"),
                description=td.get("title", ""),  # short desc from title
                agent_role=td.get("agent_role", "builder"),
                sequence_num=seq,
                estimated_minutes=td.get("estimated_minutes", 30),
                files_likely_touched=td.get("files_likely_touched", []),
                branch_name=_epic_branch_name(epic.get("title", "epic"), str(self.run_id)),
                status="PENDING",
                created_at=datetime.now(UTC),
            )
            session.add(task)
            task_rows.append(task)

        # Write TaskDependency rows
        dep_rows: list[TaskDependency] = []
        for td in tasks_data:
            task_seq = td.get("sequence_num", 0)
            task_id = seq_to_id.get(task_seq)
            if not task_id:
                continue
            for dep in td.get("dependencies", []):
                dep_seq = dep.get("depends_on_seq")
                dep_type = dep.get("dep_type", "full")
                upstream_id = seq_to_id.get(dep_seq)
                if upstream_id is None:
                    continue
                dep_row = TaskDependency(
                    id=str(uuid.uuid4()),
                    task_id=task_id,
                    depends_on_id=upstream_id,
                    dependency_type=dep_type,
                    created_at=datetime.now(UTC),
                )
                session.add(dep_row)
                dep_rows.append(dep_row)

        await session.commit()

        # ── Inject integration_wiring + verifier tasks ───────────────────────
        # DAG shape: all builders → integration_wiring → verifier
        # Planning hint {tech_stack} is stored in task.output so agents can
        # read it at execute time without re-querying PM.
        builder_ids = [t.id for t in task_rows if t.agent_role == "builder"]
        if builder_ids:
            planning_hint = {"tech_stack": tech_stack, "app_type": app_type}
            last_seq = max(t.sequence_num for t in task_rows)

            # ── integration_wiring task ──────────────────────────────────────
            wiring_seq = last_seq + 1
            wiring_id = str(uuid.uuid4())
            wiring_task = Task(
                id=wiring_id,
                run_id=str(self.run_id),
                epic_id=task_rows[0].epic_id,
                title="Wire entry points across epics",
                description="Assemble entry-point files (page.tsx, main.py, App.tsx) from all builder outputs",
                agent_role="integration_wiring",
                sequence_num=wiring_seq,
                estimated_minutes=3,
                files_likely_touched=[],
                branch_name=task_rows[0].branch_name,
                status="PENDING",
                output=planning_hint,
                created_at=datetime.now(UTC),
            )
            session.add(wiring_task)
            wiring_deps = [
                TaskDependency(
                    id=str(uuid.uuid4()),
                    task_id=wiring_id,
                    depends_on_id=bid,
                    dependency_type="full",
                    created_at=datetime.now(UTC),
                )
                for bid in builder_ids
            ]
            for dep in wiring_deps:
                session.add(dep)

            # ── verifier task (depends on integration_wiring) ────────────────
            verifier_seq = wiring_seq + 1
            verifier_id = str(uuid.uuid4())
            verifier_task = Task(
                id=verifier_id,
                run_id=str(self.run_id),
                epic_id=task_rows[0].epic_id,
                title="Verify build compiles and renders correctly",
                description="Run profile-based build verification: install, compile, typecheck, entry-point check",
                agent_role="verifier",
                sequence_num=verifier_seq,
                estimated_minutes=5,
                files_likely_touched=[],
                branch_name=task_rows[0].branch_name,
                status="PENDING",
                output=planning_hint,
                created_at=datetime.now(UTC),
            )
            session.add(verifier_task)
            verifier_dep = TaskDependency(
                id=str(uuid.uuid4()),
                task_id=verifier_id,
                depends_on_id=wiring_id,
                dependency_type="full",
                created_at=datetime.now(UTC),
            )
            session.add(verifier_dep)

            await session.commit()
            task_rows.extend([wiring_task, verifier_task])
            dep_rows.extend(wiring_deps + [verifier_dep])
            self._log.info(
                "tech_lead.post_build_tasks_injected",
                wiring_id=wiring_id,
                verifier_id=verifier_id,
                tech_stack=tech_stack,
                depends_on_builders=builder_ids,
            )

        # Compute critical path via DAG resolver
        dag_nodes: dict[str, DagNode] = {}
        for task in task_rows:
            dag_nodes[task.id] = DagNode(
                task_id=task.id,
                agent_role=task.agent_role,
                estimated_minutes=task.estimated_minutes or 30,
            )
        for dep in dep_rows:
            if dep.task_id in dag_nodes:
                dag_nodes[dep.task_id].deps[dep.depends_on_id] = dep.dependency_type

        resolver = DagResolver()
        try:
            plan = resolver.resolve(dag_nodes)
            critical_path = plan.critical_path_minutes
        except Exception:
            critical_path = sum(t.estimated_minutes or 30 for t in task_rows)

        output = {
            "tasks": [
                {
                    "id": t.id,
                    "epic_id": t.epic_id,
                    "title": t.title,
                    "agent_role": t.agent_role,
                    "sequence_num": t.sequence_num,
                    "estimated_minutes": t.estimated_minutes,
                    "branch_name": t.branch_name,
                    "dependencies": [
                        {"depends_on_id": d.depends_on_id, "dep_type": d.dependency_type}
                        for d in dep_rows
                        if d.task_id == t.id
                    ],
                }
                for t in task_rows
            ],
            "api_contract": parsed.get("api_contract", {}),
            "db_schema": parsed.get("db_schema", {}),
            "critical_path_minutes": critical_path,
        }

        self._log.info(
            "tech_lead.execute.done",
            task_count=len(task_rows),
            dep_count=len(dep_rows),
            critical_path_minutes=critical_path,
            tokens_used=self._tokens_used,
        )
        return AgentResult(success=True, output=output, tokens_used=self._tokens_used)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _epic_branch_name(epic_title: str, run_id: str) -> str:
    """
    Derive a git branch name from an epic title and run_id.
    Format: feat/<slug>-<run_id[:8]>
    Example: feat/core-infrastructure-authentication-a1b2c3d4
    """
    import re  # noqa: PLC0415
    slug = re.sub(r"[^a-z0-9]+", "-", epic_title.lower()).strip("-")[:40]
    return f"feat/{slug}-{run_id[:8]}"


def _extract_json(text: str) -> dict:
    """Extract JSON from Claude response, stripping any accidental markdown."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()
    return json.loads(text)
