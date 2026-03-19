"""
Memory Reader — retrieves relevant context from Postgres for agent injection.

Design (evidence in EXECUTION_PLAN.md §B, AD-003):
  - Primary retrieval: standing facts (always loaded), recency, confidence.
  - Secondary: pgvector semantic similarity (when embedding available).
  - Returns ranked MemoryFact/MemoryDecision lists — assembler builds the prompt block.
  - No vector DB — pgvector is an extension in the same Postgres instance.

Evidence for pgvector approach:
  pgvector GitHub: https://github.com/pgvector/pgvector
  Vector similarity search is a SQL query — no extra service, no extra latency hop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, desc, select

from forge.db.models import MemoryDecision, MemoryFact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# Maximum facts/decisions returned per query (before assembly budget trimming)
_MAX_FACTS = 20
_MAX_DECISIONS = 10


class MemoryReader:
    """
    Retrieves relevant memory entries for a given project + query context.

    Usage:
        reader = MemoryReader(session, project_id="uuid")
        facts = await reader.get_standing_facts()
        recent = await reader.get_recent_facts(limit=5)
    """

    def __init__(self, session: AsyncSession, project_id: str) -> None:
        self._session = session
        self.project_id = project_id

    async def get_standing_facts(self) -> list[MemoryFact]:
        """
        Return all is_standing=True facts for the project.
        These are ALWAYS loaded — they are the team's core invariants.
        """
        stmt = (
            select(MemoryFact)
            .where(
                MemoryFact.project_id == self.project_id,
                MemoryFact.is_standing.is_(True),
                MemoryFact.status == "confirmed",
            )
            .order_by(desc(MemoryFact.confidence), desc(MemoryFact.created_at))
        )
        result = await self._session.execute(stmt)
        facts = list(result.scalars())
        log.debug("memory_reader.standing_facts", count=len(facts))
        return facts

    async def get_standing_decisions(self) -> list[MemoryDecision]:
        """Return all standing architectural/product decisions."""
        stmt = (
            select(MemoryDecision)
            .where(
                MemoryDecision.project_id == self.project_id,
                MemoryDecision.is_standing.is_(True),
            )
            .order_by(desc(MemoryDecision.created_at))
            .limit(_MAX_DECISIONS)
        )
        result = await self._session.execute(stmt)
        decisions = list(result.scalars())
        log.debug("memory_reader.standing_decisions", count=len(decisions))
        return decisions

    async def get_recent_facts(
        self,
        limit: int = 10,
        fact_types: list[str] | None = None,
        source_run_id: str | None = None,
    ) -> list[MemoryFact]:
        """
        Return the most recent confirmed facts, optionally filtered by type or run.
        Used to give agents context about what was recently discovered.
        """
        conditions = [
            MemoryFact.project_id == self.project_id,
            MemoryFact.status == "confirmed",
        ]
        if fact_types:
            conditions.append(MemoryFact.fact_type.in_(fact_types))
        if source_run_id:
            conditions.append(MemoryFact.source_run_id == source_run_id)

        stmt = (
            select(MemoryFact)
            .where(and_(*conditions))
            .order_by(desc(MemoryFact.relevance_score), desc(MemoryFact.created_at))
            .limit(min(limit, _MAX_FACTS))
        )
        result = await self._session.execute(stmt)
        facts = list(result.scalars())
        log.debug("memory_reader.recent_facts", count=len(facts), types=fact_types)
        return facts

    async def get_facts_by_type(
        self,
        fact_type: str,
        limit: int = 10,
    ) -> list[MemoryFact]:
        """Return confirmed facts of a specific type, ordered by confidence."""
        stmt = (
            select(MemoryFact)
            .where(
                MemoryFact.project_id == self.project_id,
                MemoryFact.fact_type == fact_type,
                MemoryFact.status == "confirmed",
            )
            .order_by(desc(MemoryFact.confidence), desc(MemoryFact.relevance_score))
            .limit(min(limit, _MAX_FACTS))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars())
