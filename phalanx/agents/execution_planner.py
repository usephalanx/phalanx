"""
ExecutionPlanner — Stage 2 of the Phalanx front-door pipeline.

Takes the NormalizedSpec from Stage 1 and produces a concrete execution plan:
phases, ordered tasks, acceptance criteria, repo actions, and verification steps.

Design principle: do NOT expand scope beyond the normalized package.
Prefer the smallest implementation that satisfies the goal.

Note: named ExecutionPlanner (not Planner) to avoid collision with
phalanx/agents/planner.py which is the in-pipeline task planner agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from phalanx.agents.openai_client import OpenAIClient

if TYPE_CHECKING:
    from phalanx.agents.requirement_normalizer import NormalizedSpec

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are the Planner for Phalanx.

Your job is to turn a normalized requirement package into a concrete execution plan.

You must produce a plan that is:
- incremental
- reviewable
- low-risk
- aligned to the normalized requirements
- suitable for an engineering execution system

Rules:
- Do NOT expand scope beyond the normalized package.
- Do NOT invent hidden dependencies unless clearly required.
- Prefer the smallest implementation that satisfies the goal.
- Break work into phases and ordered tasks.
- Include validation steps.
- Respect explicit constraints and out-of-scope items.
- If the request is for a simple demo artifact, keep the plan lightweight.
- If the request is production-like, still prefer a safe MVP-first execution sequence.

Return valid JSON only.

JSON schema:
{
  "plan_summary": "string",
  "execution_strategy": "single_pass | phased_delivery | scaffold_then_refine",
  "phases": [
    {
      "phase_name": "string",
      "goal": "string",
      "tasks": [
        {
          "task_id": "string",
          "title": "string",
          "description": "string",
          "owner_role": "product | design | engineer | qa | release",
          "depends_on": ["string"],
          "acceptance_criteria": ["string"],
          "artifacts": ["string"],
          "risk_level": "low | medium | high"
        }
      ]
    }
  ],
  "repo_actions": {
    "create_branch": true,
    "branch_name_suggestion": "string",
    "commit_strategy": ["string"]
  },
  "verification_plan": {
    "build_checks": ["string"],
    "test_checks": ["string"],
    "manual_review_steps": ["string"]
  },
  "open_questions": ["string"],
  "stop_conditions": ["string"]
}"""

# Map Planner owner_role → FORGE agent_role
_ROLE_MAP = {
    "engineer": "builder",
    "qa": "qa",
    "release": "release",
    "design": "builder",
    "product": "builder",
}
_ROLE_TITLES = {
    "engineer": "Senior Software Engineer",
    "qa": "Senior QA Engineer",
    "release": "Release Engineer",
    "design": "Senior Product Designer",
    "product": "Senior Product Manager",
}


@dataclass
class PlanTask:
    task_id: str
    title: str
    description: str
    owner_role: str
    depends_on: list[str]
    acceptance_criteria: list[str]
    artifacts: list[str]
    risk_level: str


@dataclass
class PlanPhase:
    phase_name: str
    goal: str
    tasks: list[PlanTask]


@dataclass
class ExecutionPlan:
    """Concrete execution plan from ExecutionPlanner."""

    plan_summary: str
    execution_strategy: str
    phases: list[PlanPhase]
    repo_actions: dict[str, Any]
    verification_plan: dict[str, list[str]]
    open_questions: list[str]
    stop_conditions: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def all_tasks(self) -> list[PlanTask]:
        """Flat list of all tasks across all phases, in order."""
        return [task for phase in self.phases for task in phase.tasks]

    def to_enriched_spec(self) -> dict[str, Any]:
        """
        Convert to WorkOrder.enriched_spec format that Commander reads.

        Each PlanTask becomes one PhaseSpec-compatible entry.
        Commander iterates phases[0], phases[1], ... by current_phase index.
        """
        phases_out = []
        for phase in self.phases:
            for task in phase.tasks:
                phases_out.append(
                    {
                        "id": len(phases_out) + 1,
                        "name": task.title,
                        "phase_name": phase.phase_name,
                        "agent_role": _ROLE_MAP.get(task.owner_role, "builder"),
                        "role": {
                            "title": _ROLE_TITLES.get(task.owner_role, "Senior Software Engineer"),
                            "seniority": "Senior",
                            "domain": task.owner_role,
                            "persona": "",
                        },
                        "context": phase.goal,
                        "objectives": task.acceptance_criteria,
                        "deliverables": [{"file": a, "description": a} for a in task.artifacts],
                        "acceptance_criteria": task.acceptance_criteria,
                        "rules": {"do": [], "dont": []},
                        "claude_prompt": _build_claude_prompt(task, phase),
                        "_task_id": task.task_id,
                        "_risk_level": task.risk_level,
                        "_depends_on": task.depends_on,
                    }
                )

        return {
            "phases": phases_out,
            "plan_summary": self.plan_summary,
            "execution_strategy": self.execution_strategy,
            "repo_actions": self.repo_actions,
            "verification_plan": self.verification_plan,
        }


def _build_claude_prompt(task: PlanTask, phase: PlanPhase) -> str:
    """Build the claude_prompt string the Builder receives for this task."""
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria) or "- Implement as described"
    artifacts = "\n".join(f"- {a}" for a in task.artifacts) or "- See task description"
    return (
        f"[PHASE] {phase.phase_name}\n"
        f"[GOAL] {phase.goal}\n\n"
        f"[TASK] {task.title}\n\n"
        f"{task.description}\n\n"
        f"[ACCEPTANCE CRITERIA]\n{criteria}\n\n"
        f"[DELIVERABLES]\n{artifacts}\n\n"
        "Implement all deliverables above. Write complete, production-ready code. "
        "Do not skip any file listed in deliverables."
    )


class ExecutionPlanner:
    """
    Stage 2 — converts NormalizedSpec into a concrete ExecutionPlan.
    Output feeds into DryRunValidator (Stage 3) then persisted to WorkOrder.
    """

    def __init__(self) -> None:
        self._client = OpenAIClient()
        self._log = log.bind(step="execution_planning")

    def plan(self, normalized: NormalizedSpec) -> ExecutionPlan:
        """
        Generate a concrete execution plan from a NormalizedSpec.

        Args:
            normalized: Output from RequirementNormalizer (Stage 1).

        Returns:
            ExecutionPlan with phases, tasks, repo actions, verification steps.
        """
        self._log.info(
            "execution_planner.start",
            artifact_type=normalized.artifact_type,
            execution_mode=normalized.execution_mode,
            functional_reqs=len(normalized.functional_requirements),
        )

        user_content = json.dumps({"normalized_requirements": normalized.to_dict()}, indent=2)

        raw = self._client.call(
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM_PROMPT,
            max_tokens=4096,
            temperature=0.2,
        )

        phases = []
        for phase_raw in raw.get("phases", []):
            tasks = [
                PlanTask(
                    task_id=t.get("task_id", ""),
                    title=t.get("title", ""),
                    description=t.get("description", ""),
                    owner_role=t.get("owner_role", "engineer"),
                    depends_on=t.get("depends_on", []),
                    acceptance_criteria=t.get("acceptance_criteria", []),
                    artifacts=t.get("artifacts", []),
                    risk_level=t.get("risk_level", "low"),
                )
                for t in phase_raw.get("tasks", [])
            ]
            phases.append(
                PlanPhase(
                    phase_name=phase_raw.get("phase_name", ""),
                    goal=phase_raw.get("goal", ""),
                    tasks=tasks,
                )
            )

        result = ExecutionPlan(
            plan_summary=raw.get("plan_summary", ""),
            execution_strategy=raw.get("execution_strategy", "phased_delivery"),
            phases=phases,
            repo_actions=raw.get("repo_actions", {}),
            verification_plan=raw.get("verification_plan", {}),
            open_questions=raw.get("open_questions", []),
            stop_conditions=raw.get("stop_conditions", []),
            raw=raw,
        )

        self._log.info(
            "execution_planner.done",
            phases_count=len(result.phases),
            total_tasks=len(result.all_tasks),
            execution_strategy=result.execution_strategy,
        )

        return result
