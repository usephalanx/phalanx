"""Unit tests for MemoryWriter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.memory.writer import MemoryWriter


def make_session(existing_fact=None):
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_fact
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


class TestMemoryWriter:
    @pytest.mark.asyncio
    async def test_write_fact_creates_new_fact(self):
        session = make_session(existing_fact=None)
        writer = MemoryWriter(session, project_id="proj-1")

        fact = await writer.write_fact(
            fact_type="architecture",
            title="Uses async SQLAlchemy",
            body="All DB calls use AsyncSession via get_db().",
        )

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        assert fact.fact_type == "architecture"
        assert fact.title == "Uses async SQLAlchemy"
        assert fact.version == 1
        assert fact.status == "confirmed"

    @pytest.mark.asyncio
    async def test_write_fact_supersedes_existing(self):
        existing = MagicMock()
        existing.version = 1
        existing.id = "existing-id"
        existing.status = "confirmed"
        existing.superseded_by = None

        session = make_session(existing_fact=existing)
        writer = MemoryWriter(session, project_id="proj-1")

        fact = await writer.write_fact(
            fact_type="architecture",
            title="Uses async SQLAlchemy",
            body="Updated: uses NullPool in workers.",
        )

        assert existing.status == "superseded"
        assert fact.version == 2
        assert existing.superseded_by == fact.id

    @pytest.mark.asyncio
    async def test_write_decision_creates_decision(self):
        session = make_session()
        writer = MemoryWriter(session, project_id="proj-1")

        dec = await writer.write_decision(
            title="Use PostgreSQL over SQLite",
            decision="PostgreSQL with pgvector for semantic search",
            rationale="Supports vector embeddings natively",
            is_standing=True,
        )

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        assert dec.title == "Use PostgreSQL over SQLite"
        assert dec.is_standing is True

    @pytest.mark.asyncio
    async def test_write_fact_with_tags(self):
        session = make_session()
        writer = MemoryWriter(session, project_id="proj-1")

        fact = await writer.write_fact(
            fact_type="convention",
            title="No raw SQL",
            body="Always use ORM queries.",
            tags=["db", "style"],
        )

        assert fact.tags == ["db", "style"]

    @pytest.mark.asyncio
    async def test_write_decision_with_rejected_alternatives(self):
        session = make_session()
        writer = MemoryWriter(session, project_id="proj-1")

        dec = await writer.write_decision(
            title="Use Celery for task queue",
            decision="Celery with Redis broker",
            rejected_alternatives=["RQ", "Dramatiq"],
        )

        assert dec.rejected_alternatives == ["RQ", "Dramatiq"]
