"""v1.7.3 append-mode — shadow_ledger never overwrites prior evidence.

The 5 test criteria from the spec:
  1. first shadow run for a (repo, workflow_run_id) creates attempt_number=1
  2. second run on same repo/workflow creates attempt_number=2
  3. prior row is unchanged
  4. export includes both rows
  5. metrics can distinguish 'every attempt' counts from 'unique workflow' counts

Tests use a real in-memory SQLite database with the production model
schema so the actual UNIQUE(repo, workflow_run_id, attempt_number)
constraint is exercised end-to-end. No mocks, no patches on CRUD.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from phalanx.db.models import Base, ShadowLedger
from phalanx.shadow import ledger as ledger_crud


# ── fixture: real in-mem async SQLite with the prod schema ────────────


# Raw DDL for shadow_ledger only (sidesteps Base.metadata.create_all
# which would try to render Postgres-only JSONB on every other table
# in the prod metadata against SQLite).
_SHADOW_LEDGER_DDL = """
CREATE TABLE shadow_ledger (
    id TEXT PRIMARY KEY,
    repo VARCHAR(255) NOT NULL,
    workflow_run_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    pr_number INTEGER,
    failing_commit_sha CHAR(40),
    failure_class VARCHAR(40),
    phalanx_run_id TEXT,
    phalanx_verdict VARCHAR(40),
    phalanx_confidence REAL,
    phalanx_proposed_patch TEXT,
    phalanx_root_cause TEXT,
    phalanx_affected_files TEXT,
    phalanx_iterations INTEGER,
    phalanx_tool_calls INTEGER,
    phalanx_cost_usd REAL,
    phalanx_run_seconds INTEGER,
    ground_truth_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    maintainer_fix_commit_sha CHAR(40),
    maintainer_actual_patch TEXT,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (repo, workflow_run_id, attempt_number)
);
"""


@pytest.fixture
def session_factory():
    """In-memory async-SQLite with only the shadow_ledger table created
    from raw DDL — sidesteps the prod JSONB types we don't need for
    these tests. The real UNIQUE(repo, workflow_run_id, attempt_number)
    constraint is enforced and exercised end-to-end."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    async def _setup():
        async with engine.begin() as conn:
            await conn.execute(text(_SHADOW_LEDGER_DDL))

    asyncio.run(_setup())
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    asyncio.run(engine.dispose())


# ── 5 spec criteria ──────────────────────────────────────────────────


def _run(session_factory, coro_factory):
    """Run an async coroutine inside a fresh session, return result."""
    async def _go():
        async with session_factory() as session:
            return await coro_factory(session)

    return asyncio.run(_go())


class TestAppendMode:
    def test_1_first_run_creates_attempt_number_one(self, session_factory):
        async def _go(session):
            row = await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
            )
            return row

        row = _run(session_factory, _go)
        assert row.attempt_number == 1
        assert row.phalanx_verdict == "PENDING"

    def test_2_second_run_on_same_workflow_creates_attempt_two(
        self, session_factory
    ):
        async def _go(session):
            r1 = await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
            )
            r2 = await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
            )
            return r1, r2

        r1, r2 = _run(session_factory, _go)
        assert r1.attempt_number == 1
        assert r2.attempt_number == 2
        assert r1.id != r2.id  # distinct ledger rows

    def test_3_prior_row_unchanged_when_second_run_appends(
        self, session_factory
    ):
        async def _go(session):
            r1 = await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
                phalanx_run_id="11111111-1111-1111-1111-111111111111",
            )
            # Simulate completing the first run
            await ledger_crud.update_with_results(
                session,
                ledger_id=r1.id,
                verdict="SHIPPED_PROPOSED",
                confidence=0.85,
                proposed_patch="--- a/x\n+++ b/x\n",
                root_cause="rc1",
                affected_files=["x.py"],
                iterations=1,
                tool_calls=7,
                cost_usd=2.10,
                run_seconds=600,
            )

            # Second invocation on same workflow appends.
            r2 = await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
                phalanx_run_id="22222222-2222-2222-2222-222222222222",
            )

            # Re-fetch r1 to confirm nothing was touched on it.
            r1_after = await ledger_crud.get(session, r1.id)
            return r1_after, r2

        r1_after, r2 = _run(session_factory, _go)
        # r1 retains its original SHIPPED_PROPOSED verdict
        assert r1_after.phalanx_verdict == "SHIPPED_PROPOSED"
        assert r1_after.phalanx_confidence == 0.85
        assert r1_after.phalanx_proposed_patch == "--- a/x\n+++ b/x\n"
        assert r1_after.phalanx_run_id == "11111111-1111-1111-1111-111111111111"
        assert r1_after.attempt_number == 1
        # r2 is the new pending append
        assert r2.attempt_number == 2
        assert r2.phalanx_verdict == "PENDING"
        assert r2.phalanx_run_id == "22222222-2222-2222-2222-222222222222"

    def test_4_export_includes_both_rows(self, session_factory):
        async def _go(session):
            await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
            )
            await ledger_crud.create_pending(
                session,
                repo="encode/httpx",
                workflow_run_id=12345,
                pr_number=100,
                failing_commit_sha="a" * 40,
            )
            return await ledger_crud.list_all(session)

        rows = _run(session_factory, _go)
        # Both attempts present
        assert len(rows) == 2
        attempt_numbers = sorted(r.attempt_number for r in rows)
        assert attempt_numbers == [1, 2]
        # to_dict shape includes attempt_number
        for r in rows:
            d = ledger_crud.to_dict(r)
            assert "attempt_number" in d
            json.dumps(d, default=str)  # JSON-roundtrip-safe

    def test_5_metrics_distinguish_attempts_from_unique_workflows(
        self, session_factory
    ):
        """list_all returns every attempt; latest_per_workflow returns
        one row per (repo, workflow_run_id). The CLI metrics command
        keys off these two helpers."""
        async def _go(session):
            # Workflow A — 2 attempts (FAILED then SHIPPED_PROPOSED)
            a1 = await ledger_crud.create_pending(
                session, repo="r/a", workflow_run_id=1,
                pr_number=None, failing_commit_sha=None,
            )
            await ledger_crud.update_with_results(
                session, ledger_id=a1.id,
                verdict="FAILED", confidence=0.0,
                proposed_patch=None, root_cause=None,
                affected_files=None, iterations=1,
                tool_calls=5, cost_usd=0.10, run_seconds=120,
            )
            a2 = await ledger_crud.create_pending(
                session, repo="r/a", workflow_run_id=1,
                pr_number=None, failing_commit_sha=None,
            )
            await ledger_crud.update_with_results(
                session, ledger_id=a2.id,
                verdict="SHIPPED_PROPOSED", confidence=0.8,
                proposed_patch="diff", root_cause="rc",
                affected_files=["x.py"], iterations=1,
                tool_calls=6, cost_usd=0.15, run_seconds=180,
            )

            # Workflow B — 1 attempt (SAFE_ESCALATE)
            b1 = await ledger_crud.create_pending(
                session, repo="r/b", workflow_run_id=2,
                pr_number=None, failing_commit_sha=None,
            )
            await ledger_crud.update_with_results(
                session, ledger_id=b1.id,
                verdict="SAFE_ESCALATE", confidence=0.0,
                proposed_patch=None, root_cause=None,
                affected_files=None, iterations=1,
                tool_calls=4, cost_usd=0.08, run_seconds=90,
            )

            all_rows = await ledger_crud.list_all(session)
            latest_rows = await ledger_crud.latest_per_workflow(session)
            return all_rows, latest_rows

        all_rows, latest_rows = _run(session_factory, _go)

        # Attempts = 3 total (workflow A counted twice — once per attempt)
        assert len(all_rows) == 3
        attempt_verdicts = sorted(r.phalanx_verdict for r in all_rows)
        assert attempt_verdicts == ["FAILED", "SAFE_ESCALATE", "SHIPPED_PROPOSED"]

        # Unique workflows = 2 (only the LATEST attempt per workflow)
        assert len(latest_rows) == 2
        latest_verdicts = sorted(r.phalanx_verdict for r in latest_rows)
        assert latest_verdicts == ["SAFE_ESCALATE", "SHIPPED_PROPOSED"]
        # Critically: workflow A's latest attempt is SHIPPED_PROPOSED,
        # NOT FAILED — proves we picked attempt_number=2 (not the
        # earlier FAILED attempt #1).
        wf_a_latest = next(r for r in latest_rows if r.workflow_run_id == 1)
        assert wf_a_latest.phalanx_verdict == "SHIPPED_PROPOSED"
        assert wf_a_latest.attempt_number == 2


# ── unique constraint exercised end-to-end ──────────────────────────


class TestUniqueConstraintShape:
    def test_three_attempts_serialize_under_unique(self, session_factory):
        """Sanity: rapid sequential appends on the same workflow produce
        attempts 1, 2, 3 — never collide on the unique."""
        async def _go(session):
            ids = []
            for _ in range(3):
                row = await ledger_crud.create_pending(
                    session,
                    repo="r/x",
                    workflow_run_id=99,
                    pr_number=None,
                    failing_commit_sha=None,
                )
                ids.append((row.id, row.attempt_number))
            return ids

        ids = _run(session_factory, _go)
        attempts = sorted(att for _, att in ids)
        assert attempts == [1, 2, 3]
        # All three rows are distinct
        assert len({rid for rid, _ in ids}) == 3

    def test_different_workflows_independent_attempt_counters(
        self, session_factory
    ):
        async def _go(session):
            a = await ledger_crud.create_pending(
                session, repo="r/a", workflow_run_id=1,
                pr_number=None, failing_commit_sha=None,
            )
            b = await ledger_crud.create_pending(
                session, repo="r/a", workflow_run_id=2,
                pr_number=None, failing_commit_sha=None,
            )
            return a, b

        a, b = _run(session_factory, _go)
        # Two DIFFERENT workflow_run_ids each start at 1 independently.
        assert a.attempt_number == 1
        assert b.attempt_number == 1
        assert a.id != b.id


# ── list_attempts_for_workflow helper ────────────────────────────────


class TestListAttemptsForWorkflow:
    def test_returns_attempts_in_order(self, session_factory):
        async def _go(session):
            for _ in range(3):
                await ledger_crud.create_pending(
                    session, repo="r/x", workflow_run_id=42,
                    pr_number=None, failing_commit_sha=None,
                )
            # And a different workflow on same repo (must NOT be returned)
            await ledger_crud.create_pending(
                session, repo="r/x", workflow_run_id=99,
                pr_number=None, failing_commit_sha=None,
            )
            return await ledger_crud.list_attempts_for_workflow(
                session, repo="r/x", workflow_run_id=42,
            )

        rows = _run(session_factory, _go)
        assert [r.attempt_number for r in rows] == [1, 2, 3]
        assert all(r.workflow_run_id == 42 for r in rows)
