"""
Memory Writer — persists structured facts and decisions to Postgres.

Design (evidence in EXECUTION_PLAN.md §B, AD-003):
  - MemoryFact: durable project knowledge (architecture, constraints, conventions).
  - MemoryDecision: approved architectural/product decisions (always loaded when standing).
  - Content is deduplicated by title+fact_type — existing rows are updated (versioned).
  - No external vector DB — pgvector is embedded in Postgres (zero new infra).
  - Embeddings generated via Anthropic API; None is valid (semantic search is optional).

AP-003: Exceptions from DB writes propagate to caller — never swallowed here.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.models import MemoryDecision, MemoryFact

log = structlog.get_logger(__name__)


class MemoryWriter:
    """
    Writes facts and decisions to Postgres memory tables.

    Usage:
        writer = MemoryWriter(session, project_id="uuid")
        await writer.write_fact(
            fact_type="architecture",
            title="Uses async SQLAlchemy 2.0",
            body="All DB calls use AsyncSession via get_db() context manager.",
            source_run_id="uuid",
        )
    """

    def __init__(self, session: AsyncSession, project_id: str) -> None:
        self._session = session
        self.project_id = project_id

    async def write_fact(
        self,
        fact_type: str,
        title: str,
        body: str,
        confidence: float = 1.0,
        is_standing: bool = False,
        source_run_id: Optional[str] = None,
        source_artifact_id: Optional[str] = None,
        tags: list[str] | None = None,
    ) -> MemoryFact:
        """
        Persist a MemoryFact. If a fact with the same project+title+fact_type
        already exists, increment its version and mark the old one superseded.

        Returns the newly created/updated MemoryFact.
        """
        from sqlalchemy import select  # noqa: PLC0415

        # Check for existing fact with same title/type
        stmt = select(MemoryFact).where(
            MemoryFact.project_id == self.project_id,
            MemoryFact.title == title,
            MemoryFact.fact_type == fact_type,
            MemoryFact.status != "superseded",
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()

        new_version = 1
        if existing:
            # Mark old fact as superseded
            existing.status = "superseded"
            new_version = existing.version + 1

        fact = MemoryFact(
            project_id=self.project_id,
            fact_type=fact_type,
            title=title,
            body=body,
            confidence=confidence,
            status="confirmed",
            version=new_version,
            is_standing=is_standing,
            superseded_by=None,
            source_run_id=source_run_id,
            source_artifact_id=source_artifact_id,
            relevance_score=1.0,
            tags=tags or [],
        )
        self._session.add(fact)
        await self._session.flush()  # get the ID without committing

        if existing:
            existing.superseded_by = fact.id

        log.info(
            "memory_writer.fact_written",
            project_id=self.project_id,
            fact_id=fact.id,
            fact_type=fact_type,
            title=title,
            version=new_version,
        )

        return fact

    async def write_decision(
        self,
        title: str,
        decision: str,
        rationale: Optional[str] = None,
        rejected_alternatives: list[str] | None = None,
        decided_by: Optional[str] = None,
        is_standing: bool = True,
        approval_id: Optional[str] = None,
    ) -> MemoryDecision:
        """
        Persist an approved architectural or product decision.
        Standing decisions (is_standing=True) are always loaded into agent context.
        """
        dec = MemoryDecision(
            project_id=self.project_id,
            title=title,
            decision=decision,
            rationale=rationale,
            rejected_alternatives=rejected_alternatives or [],
            decided_by=decided_by,
            is_standing=is_standing,
            approval_id=approval_id,
        )
        self._session.add(dec)
        await self._session.flush()

        log.info(
            "memory_writer.decision_written",
            project_id=self.project_id,
            decision_id=dec.id,
            title=title,
            is_standing=is_standing,
        )

        return dec
