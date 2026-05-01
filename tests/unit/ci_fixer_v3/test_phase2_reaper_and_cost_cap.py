"""Tier-1 tests for Phase 2 — reaper + per-run cost cap.

Reaper: replaces the prior `check_blocked_runs` stub. Targets v3 ci_fix
runs in EXECUTING/VERIFYING with `updated_at` older than the threshold;
flips them + their in-flight child Tasks to FAILED.

Cost cap: in cifix_commander, before each iteration dispatch, sums
tasks.tokens_used and aborts the Run if the estimate exceeds the cap.

Both tests mock get_db so they run sub-second without Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from phalanx.agents import cifix_commander as cc_mod
from phalanx.maintenance import tasks as maint_mod
from phalanx.maintenance.tasks import (
    STUCK_RUN_THRESHOLD_MINUTES,
    _check_blocked_runs_impl,
)


# ─────────────────────────────────────────────────────────────────────
# Reaper tests
# ─────────────────────────────────────────────────────────────────────


class _FakeRun:
    def __init__(self, id_: str, status: str, age_minutes: int):
        self.id = id_
        self.status = status
        self.updated_at = datetime.now(UTC) - timedelta(minutes=age_minutes)


def _patch_get_db_with_runs(monkeypatch, runs):
    """Wire maint_mod.get_db to return a session whose first execute() yields
    `runs` via .scalars().all(); subsequent execute() calls (the UPDATEs) are
    swallowed."""
    fake_session = MagicMock()
    state = {"calls": 0}

    async def fake_execute(*_a, **_k):
        state["calls"] += 1
        result = MagicMock()
        if state["calls"] == 1:
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=runs)
            result.scalars = MagicMock(return_value=scalars)
        return result

    fake_session.execute = fake_execute
    fake_session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_session

    monkeypatch.setattr(maint_mod, "get_db", fake_get_db)
    return fake_session


async def test_reaper_returns_zero_when_no_stuck_runs(monkeypatch):
    _patch_get_db_with_runs(monkeypatch, [])
    result = await _check_blocked_runs_impl()
    assert result == {"killed": 0}


async def test_reaper_kills_runs_older_than_threshold(monkeypatch):
    """The reaper must mark FAILED both the Run and any IN_PROGRESS/PENDING
    child Tasks. Two runs at +35min and +60min — both should be killed."""
    runs = [
        _FakeRun(id_="run-1", status="EXECUTING", age_minutes=35),
        _FakeRun(id_="run-2", status="VERIFYING", age_minutes=60),
    ]
    fake_session = _patch_get_db_with_runs(monkeypatch, runs)
    result = await _check_blocked_runs_impl()
    assert result["killed"] == 2
    assert set(result["ids"]) == {"run-1", "run-2"}
    # Each killed run produces 2 UPDATEs (Run + Task) plus 1 SELECT = 5 calls.
    # Defensive: just assert commit was called once.
    assert fake_session.commit.call_count == 1


async def test_reaper_threshold_is_30_minutes():
    """Constant should not regress without intent."""
    assert STUCK_RUN_THRESHOLD_MINUTES == 30


async def test_reaper_does_not_kill_recent_runs(monkeypatch):
    """Defensive: the SQL filter is `updated_at < cutoff`. Test the
    impl returns 0 when scalars().all() comes back empty — modeling the
    'all runs are recent' DB state."""
    _patch_get_db_with_runs(monkeypatch, [])
    result = await _check_blocked_runs_impl()
    assert result["killed"] == 0


# ─────────────────────────────────────────────────────────────────────
# Cost cap tests
# ─────────────────────────────────────────────────────────────────────


def _patch_cost_aggregate(monkeypatch, total_tokens: int):
    """Make cifix_commander.get_db yield a session whose execute() returns
    a result whose .scalar() is `total_tokens`. Used by _check_cost_cap."""
    fake_session = MagicMock()

    async def fake_execute(*_a, **_k):
        result = MagicMock()
        result.scalar = MagicMock(return_value=total_tokens)
        return result

    fake_session.execute = fake_execute
    fake_session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_session

    monkeypatch.setattr(cc_mod, "get_db", fake_get_db)


def _make_commander_for_cost_check():
    """Build a minimal CIFixCommanderAgent instance for testing _check_cost_cap.
    We don't run execute(); just call the helper directly."""
    agent = cc_mod.CIFixCommanderAgent.__new__(cc_mod.CIFixCommanderAgent)
    agent.run_id = "test-run"
    agent._log = MagicMock()
    return agent


async def test_cost_cap_does_not_abort_under_threshold(monkeypatch):
    """40_000 tokens × $20e-6 = $0.80 — under the $1.00 cap; no abort."""
    _patch_cost_aggregate(monkeypatch, total_tokens=40_000)
    agent = _make_commander_for_cost_check()
    should_abort, estimate, tokens = await agent._check_cost_cap()
    assert should_abort is False
    assert tokens == 40_000
    assert 0.79 <= estimate <= 0.81


async def test_cost_cap_aborts_above_threshold(monkeypatch):
    """60_000 tokens × $20e-6 = $1.20 — over the $1.00 cap; abort=True."""
    _patch_cost_aggregate(monkeypatch, total_tokens=60_000)
    agent = _make_commander_for_cost_check()
    should_abort, estimate, tokens = await agent._check_cost_cap()
    assert should_abort is True
    assert tokens == 60_000
    assert 1.19 <= estimate <= 1.21


async def test_cost_cap_handles_zero_tokens(monkeypatch):
    _patch_cost_aggregate(monkeypatch, total_tokens=0)
    agent = _make_commander_for_cost_check()
    should_abort, estimate, tokens = await agent._check_cost_cap()
    assert should_abort is False
    assert tokens == 0
    assert estimate == 0.0


def test_cost_cap_constants_documented():
    """Sanity: constants exposed at module level so future edits + this
    test stay synced."""
    assert cc_mod._COST_PER_TOKEN_USD == 20e-6
    assert cc_mod._MAX_RUN_COST_USD == 1.0


def test_cost_cap_threshold_reasonable():
    """The cap must let the typical-cell run finish (under $0.20) but
    catch a runaway 3-iter loop (~$1.50+). $1.00 sits between."""
    typical_cell_tokens = 10_000  # ~$0.20
    runaway_tokens = 100_000  # ~$2.00
    assert typical_cell_tokens * cc_mod._COST_PER_TOKEN_USD < cc_mod._MAX_RUN_COST_USD
    assert runaway_tokens * cc_mod._COST_PER_TOKEN_USD > cc_mod._MAX_RUN_COST_USD
