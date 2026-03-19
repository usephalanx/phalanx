"""
Team Runtime — live availability and WIP tracking for all team members.

Answers: "Is agent <X> available to take a task right now?"

Design decisions:
  - Source of truth: Postgres `runs` table (active run counts per work_order owner).
  - Team config loaded from configs/team.yaml via ConfigLoader.
  - No Redis cache for WIP counts — Postgres is always authoritative.
  - Immutable TeamConfig at startup; reload explicitly via reload().

Evidence:
  The WIP-limit-per-member approach is documented in EXECUTION_PLAN.md §B.
  We query Postgres directly rather than caching in Redis to avoid stale reads
  when multiple commanders run concurrently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from forge.config.loader import ConfigLoader, TeamConfig, TeamMember
from forge.db.models import Run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# Non-terminal Run states that count toward WIP
_ACTIVE_STATUSES = frozenset(
    {
        "INTAKE",
        "RESEARCHING",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "AWAITING_SHIP_APPROVAL",
        "READY_TO_MERGE",
        "MERGED",
        "RELEASE_PREP",
        "AWAITING_RELEASE_APPROVAL",
        "BLOCKED",
        "PAUSED",
    }
)


class AgentUnavailableError(RuntimeError):
    """Raised when no agent is available for the requested role."""


class TeamRuntime:
    """
    Manages live team state: who is available, WIP limits, routing hints.

    Usage:
        runtime = TeamRuntime()
        agent = await runtime.find_available_agent(
            session=db_session,
            role="builder",
            min_ic_level=4,
        )
    """

    def __init__(self, config_loader: ConfigLoader | None = None) -> None:
        self._loader = config_loader or ConfigLoader()

    @property
    def team_config(self) -> TeamConfig:
        return self._loader.team

    def reload(self) -> None:
        """Force re-read of team.yaml on next access."""
        self._loader.reload()

    def get_members_by_role(self, role: str) -> list[TeamMember]:
        """Return all team members matching the given agent role."""
        return [m for m in self.team_config.members if m.role == role or m.id == role]

    def get_member(self, member_id: str) -> TeamMember | None:
        return self.team_config.get_member(member_id)

    async def active_run_count(
        self,
        session: AsyncSession,
        agent_id: str,
        project_id: str | None = None,
    ) -> int:
        """
        Count runs currently active for the given agent (by work_order.requested_by
        or run.project_id scoping).

        For MVP, we count all active runs project-wide assigned to this agent_id
        by looking at runs where any task has assigned_agent_id = agent_id.
        This is a conservative WIP count — tighter scoping is a future refinement.
        """
        from forge.db.models import Task  # noqa: PLC0415

        stmt = (
            select(func.count())
            .select_from(Run)
            .join(Task, Task.run_id == Run.id)
            .where(
                Task.assigned_agent_id == agent_id,
                Run.status.in_(list(_ACTIVE_STATUSES)),
            )
        )
        if project_id:
            stmt = stmt.where(Run.project_id == project_id)

        result = await session.execute(stmt)
        return result.scalar_one()

    async def find_available_agent(
        self,
        session: AsyncSession,
        role: str,
        min_ic_level: int = 3,
        project_id: str | None = None,
    ) -> TeamMember:
        """
        Find the first available team member for the given role, respecting
        WIP limits defined in team.yaml.

        Raises AgentUnavailableError if no member is available.
        Prefers lower-IC members (IC3 → IC4) when multiple are available,
        reserving senior members for complex/escalated tasks.
        """
        candidates = [
            m
            for m in self.team_config.members
            if m.ic_level >= min_ic_level and (m.role == role or m.id == role)
        ]

        if not candidates:
            raise AgentUnavailableError(
                f"No team member found for role={role!r} min_ic_level={min_ic_level}"
            )

        # Sort by IC level ascending — use junior first, keep seniors free
        candidates.sort(key=lambda m: m.ic_level)

        for member in candidates:
            wip = await self.active_run_count(session, member.id, project_id)
            if wip < member.max_concurrent_tasks:
                log.info(
                    "team_runtime.agent_selected",
                    agent_id=member.id,
                    role=role,
                    wip=wip,
                    limit=member.max_concurrent_tasks,
                )
                return member

        # All candidates at WIP limit
        ids = [m.id for m in candidates]
        raise AgentUnavailableError(
            f"All agents for role={role!r} are at WIP limit: {ids}. Retry when a run completes."
        )

    async def is_at_wip_limit(
        self,
        session: AsyncSession,
        agent_id: str,
        project_id: str | None = None,
    ) -> bool:
        """Return True if the agent has reached their max_concurrent_tasks."""
        member = self.get_member(agent_id)
        if member is None:
            return False  # unknown agent — no limit enforced
        wip = await self.active_run_count(session, agent_id, project_id)
        return wip >= member.max_concurrent_tasks
