"""
Unit tests for phalanx/workflow/advance_run.py.

Patches target module-level names in advance_run.py (get_settings, get_db,
_redis_lib, TaskRouter, validate_transition) so they can be swapped in tests.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.workflow.advance_run import _advance_run_async, _STALE_TASK_TIMEOUT_SECONDS


# ── helpers ───────────────────────────────────────────────────────────────────


def make_run(run_id="run-1", status="EXECUTING"):
    run = MagicMock()
    run.id = run_id
    run.status = status
    return run


def make_task(
    task_id="task-1",
    agent_role="builder",
    sequence_num=1,
    status="PENDING",
    started_at=None,
    assigned_agent_id=None,
    error=None,
):
    t = MagicMock()
    t.id = task_id
    t.agent_role = agent_role
    t.sequence_num = sequence_num
    t.status = status
    t.started_at = started_at
    t.assigned_agent_id = assigned_agent_id
    t.error = error
    return t


def _scalar_result(obj):
    """Mock execute() result with scalar_one_or_none and scalar_one."""
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=obj)
    r.scalar_one = MagicMock(return_value=obj)
    return r


def _scalars_result(items):
    """Mock execute() result whose .scalars() returns an iterable + list."""
    inner = MagicMock()
    inner.__iter__ = MagicMock(return_value=iter(items))
    inner.all = MagicMock(return_value=items)
    inner.first = MagicMock(return_value=items[0] if items else None)
    r = MagicMock()
    r.scalars = MagicMock(return_value=inner)
    return r


@asynccontextmanager
async def _fake_db(session):
    yield session


def _make_redis(acquired=True):
    r = MagicMock()
    r.set = MagicMock(return_value=True if acquired else None)
    r.delete = MagicMock()
    r.exists = MagicMock(return_value=0)
    return r


# ── Lock busy ─────────────────────────────────────────────────────────────────


def test_lock_busy_returns_immediately():
    """advance_run exits immediately when Redis lock is already held."""
    redis_mock = _make_redis(acquired=False)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "lock_busy"


# ── Terminal state ────────────────────────────────────────────────────────────


def test_terminal_run_exits():
    """A run in FAILED state exits without dispatching anything."""
    run = make_run(status="FAILED")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalar_result(run))
    session.commit = AsyncMock()

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "terminal"
    assert result["run_status"] == "FAILED"


# ── Run not found ─────────────────────────────────────────────────────────────


def test_run_not_found():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalar_result(None))
    session.commit = AsyncMock()

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-missing", attempt=0))

    assert result["status"] == "not_found"


# ── All tasks complete ────────────────────────────────────────────────────────


def test_all_complete_transitions_to_verifying():
    """All COMPLETED tasks → transition EXECUTING → VERIFYING."""
    run = make_run(status="EXECUTING")
    t1 = make_task(task_id="t1", status="COMPLETED")
    t2 = make_task(task_id="t2", status="COMPLETED")

    execute_calls = [
        _scalar_result(run),           # Run lookup
        _scalars_result([t1, t2]),     # Task list
        MagicMock(),                   # update Run status
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_calls)
    session.commit = AsyncMock()

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
        patch("phalanx.workflow.advance_run._schedule_recheck") as mock_recheck,
        patch("phalanx.workflow.advance_run.validate_transition"),
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "all_complete"
    mock_recheck.assert_called_once()


# ── IN_PROGRESS healthy task ──────────────────────────────────────────────────


def test_in_progress_task_reschedules():
    """Non-stale IN_PROGRESS task → waiting + reschedule."""
    run = make_run(status="EXECUTING")
    t1 = make_task(
        task_id="t1",
        status="IN_PROGRESS",
        started_at=datetime.now(UTC) - timedelta(seconds=60),
    )

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _scalar_result(run),
        _scalars_result([t1]),
    ])
    session.commit = AsyncMock()

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
        patch("phalanx.workflow.advance_run._schedule_recheck") as mock_recheck,
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "waiting"
    mock_recheck.assert_called_once()


# ── Stale task reset ──────────────────────────────────────────────────────────


def test_stale_task_gets_reset_to_pending():
    """Task IN_PROGRESS > timeout → reset to PENDING, then dispatched."""
    run = make_run(status="EXECUTING")
    stale_start = datetime.now(UTC) - timedelta(seconds=_STALE_TASK_TIMEOUT_SECONDS + 60)
    t1_stale = make_task(task_id="t1", status="IN_PROGRESS", started_at=stale_start)
    t1_reset = make_task(task_id="t1", agent_role="builder", sequence_num=1, status="PENDING")

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _scalar_result(run),          # Run lookup
        _scalars_result([t1_stale]),  # initial task list
        MagicMock(),                  # update stale task → PENDING
        _scalars_result([t1_reset]),  # reload after reset
        MagicMock(),                  # update t1 → IN_PROGRESS (dispatch)
    ])
    session.commit = AsyncMock()

    router_mock = MagicMock()
    router_mock.dispatch = MagicMock(return_value="celery-id")

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
        patch("phalanx.workflow.advance_run.TaskRouter", return_value=router_mock),
        patch("phalanx.workflow.advance_run._schedule_recheck"),
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "dispatched"
    assert result["task_id"] == "t1"


# ── Failed task → fail the run ────────────────────────────────────────────────


def test_failed_task_fails_the_run():
    """A FAILED task should transition the run to FAILED."""
    run = make_run(status="EXECUTING")
    t1 = make_task(task_id="t1", status="FAILED", error="build error")

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _scalar_result(run),
        _scalars_result([t1]),
        MagicMock(),  # update Run status → FAILED
    ])
    session.commit = AsyncMock()

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
        patch("phalanx.workflow.advance_run.validate_transition"),
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "run_failed"
    assert result["task_id"] == "t1"


# ── Dispatch next PENDING task ────────────────────────────────────────────────


def test_pending_task_dispatched():
    """Next PENDING task is marked IN_PROGRESS and dispatched to its queue."""
    run = make_run(status="EXECUTING")
    t1 = make_task(task_id="t1", agent_role="builder", sequence_num=1, status="PENDING")

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _scalar_result(run),
        _scalars_result([t1]),
        MagicMock(),  # update t1 → IN_PROGRESS
    ])
    session.commit = AsyncMock()

    router_mock = MagicMock()
    router_mock.dispatch = MagicMock(return_value="celery-task-id")

    redis_mock = _make_redis(acquired=True)

    with (
        patch("phalanx.workflow.advance_run.get_settings",
              return_value=MagicMock(redis_url="redis://localhost")),
        patch("phalanx.workflow.advance_run._redis_lib") as mock_redis_lib,
        patch("phalanx.workflow.advance_run.get_db", side_effect=lambda: _fake_db(session)),
        patch("phalanx.workflow.advance_run.TaskRouter", return_value=router_mock),
        patch("phalanx.workflow.advance_run._schedule_recheck") as mock_recheck,
    ):
        mock_redis_lib.from_url = MagicMock(return_value=redis_mock)
        result = asyncio.run(_advance_run_async("run-1", attempt=0))

    assert result["status"] == "dispatched"
    assert result["agent_role"] == "builder"
    router_mock.dispatch.assert_called_once_with(
        agent_role="builder",
        task_id="t1",
        run_id="run-1",
        payload={"assigned_agent_id": t1.assigned_agent_id},
    )
    mock_recheck.assert_called_once()
