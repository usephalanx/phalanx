"""
Product Manager Agent — decomposes a WorkOrder into epics, user stories,
and acceptance criteria.

Responsibilities:
  1. Call Claude to break the work order into 2-5 epics (component groups)
  2. Write each epic to the DB (creates Epic rows)
  3. Produce a 'user_stories' artifact for the approval message
  4. Estimate component complexity (feeds TechLead's time estimates)

Output (task.output):
  {
    "epics": [
      {"title": "Infrastructure", "description": "...", "sequence_num": 1},
      ...
    ],
    "user_stories": ["As a user I can ...", ...],
    "acceptance_criteria": ["Given ... When ... Then ...", ...],
    "app_type": "web"
  }

Design rules:
  - Uses claude-haiku (small, structured output — no complex reasoning needed)
  - Produces JSON directly from Claude — no markdown parsing
  - Epic count: min 1, max 5 (hard constraint in prompt)
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
from phalanx.db.models import Epic

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a senior product manager at a software company.
Your job is to decompose a feature request into clear, independently buildable epics.

Rules:
- Output ONLY valid JSON — no markdown, no prose, no code fences.
- 2-5 epics maximum. Each epic must be independently buildable by one developer.
- User stories follow "As a [role], I can [action] so that [benefit]" format.
- Acceptance criteria use "Given/When/Then" format.
- app_type must be one of: web | mobile | api | cli
- tech_stack must be one of: nextjs | vite | sveltekit | generic_web | fastapi | django | express | go | generic_python | react_native | expo | flutter | click_cli
"""

_USER_PROMPT_TEMPLATE = """Decompose this work order into epics:

Title: {title}
Description: {description}

Return JSON exactly in this shape:
{{
  "app_type": "web",
  "tech_stack": "nextjs",
  "epics": [
    {{
      "title": "Infrastructure",
      "description": "Database models, base app setup, auth middleware",
      "sequence_num": 1,
      "estimated_complexity": 3
    }}
  ],
  "user_stories": [
    "As a buyer, I can browse property listings so that I can find homes to buy."
  ],
  "acceptance_criteria": [
    "Given I am on the listings page, When I filter by price, Then I see only matching properties."
  ]
}}"""


class ProductManagerAgent(BaseAgent):
    """
    Decomposes a WorkOrder into epics + user stories.

    Called during the Strategy phase, before builders start.
    Unlike builder/reviewer, this agent does NOT use a Celery task —
    it is called inline from Commander during the planning phase.
    """

    AGENT_ROLE = "product_manager"

    def __init__(self, run_id: str, task_id: str | None = None) -> None:
        super().__init__(run_id=run_id, agent_id="product_manager", task_id=task_id)
        self._settings = get_settings()

    async def execute(self) -> AgentResult:
        """Not used — PM is called inline via execute_for_work_order."""
        raise NotImplementedError("Use execute_for_work_order(session, work_order) instead.")

    async def execute_for_work_order(self, session: Any, work_order: Any) -> AgentResult:
        """
        Decompose the work order into epics and user stories.

        Args:
            session: AsyncSession — for writing Epic rows
            work_order: WorkOrder ORM object

        Returns:
            AgentResult with output dict containing epics, user_stories, acceptance_criteria
        """
        self._log.info("product_manager.execute.start")

        prompt = _USER_PROMPT_TEMPLATE.format(
            title=work_order.title,
            description=work_order.description,
        )

        messages = [{"role": "user", "content": prompt}]

        try:
            response_text = self._call_claude(
                messages=messages,
                system=_SYSTEM_PROMPT,
                model=self._settings.anthropic_model_fast,  # haiku — fast + cheap
                max_tokens=2048,
            )
        except Exception as exc:
            self._log.error("product_manager.claude_call_failed", error=str(exc))
            return AgentResult(success=False, output={}, error=str(exc))

        # Parse JSON response
        try:
            parsed = _extract_json(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self._log.error("product_manager.parse_failed", error=str(exc), response=response_text[:200])
            return AgentResult(success=False, output={}, error=f"JSON parse error: {exc}")

        epics_data: list[dict] = parsed.get("epics", [])
        if not epics_data:
            return AgentResult(
                success=False,
                output={},
                error="Product manager produced no epics.",
            )

        # Clamp to 1-5 epics
        epics_data = epics_data[:5]

        # Write Epic rows to DB
        epic_rows: list[Epic] = []
        for ep in epics_data:
            epic = Epic(
                id=str(uuid.uuid4()),
                run_id=str(self.run_id),
                title=ep.get("title", "Untitled"),
                description=ep.get("description"),
                status="PENDING",
                sequence_num=ep.get("sequence_num", len(epic_rows) + 1),
                estimated_minutes=_complexity_to_minutes(ep.get("estimated_complexity", 3)),
                created_at=datetime.now(UTC),
            )
            session.add(epic)
            epic_rows.append(epic)

        await session.commit()

        output = {
            "app_type": parsed.get("app_type", "web"),
            "tech_stack": parsed.get("tech_stack", ""),  # empty = auto-detect at build time
            "epics": [
                {
                    "id": e.id,
                    "title": e.title,
                    "description": e.description,
                    "sequence_num": e.sequence_num,
                    "estimated_minutes": e.estimated_minutes,
                }
                for e in epic_rows
            ],
            "user_stories": parsed.get("user_stories", []),
            "acceptance_criteria": parsed.get("acceptance_criteria", []),
        }

        self._log.info(
            "product_manager.execute.done",
            epic_count=len(epic_rows),
            tokens_used=self._tokens_used,
        )
        return AgentResult(success=True, output=output, tokens_used=self._tokens_used)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _extract_json(text: str) -> dict:
    """Extract JSON from Claude response, stripping any accidental markdown."""
    text = text.strip()
    # Remove code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()
    return json.loads(text)


def _complexity_to_minutes(complexity: int) -> int:
    """Map 1-5 complexity score to estimated build minutes."""
    mapping = {1: 10, 2: 20, 3: 30, 4: 45, 5: 60}
    return mapping.get(int(complexity), 30)
