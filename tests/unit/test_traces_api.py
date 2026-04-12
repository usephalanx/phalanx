"""
Unit tests for GET /v1/runs/{run_id}/trace and GET /v1/runs/{run_id}/trace/{id}.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from phalanx.api.main import app


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_trace(
    trace_id="trace-1",
    run_id="run-1",
    task_id="task-1",
    agent_role="builder",
    agent_id="builder",
    trace_type="reflection",
    content="I am thinking about this task carefully.",
    context=None,
    tokens_used=50,
):
    t = MagicMock()
    t.id = trace_id
    t.run_id = run_id
    t.task_id = task_id
    t.agent_role = agent_role
    t.agent_id = agent_id
    t.trace_type = trace_type
    t.content = content
    t.context = context or {}
    t.tokens_used = tokens_used
    t.created_at = datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC)
    return t


def make_session_with_run_and_traces(run_exists: bool, traces: list):
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    # run_check result
    run_check = MagicMock()
    run_check.scalar_one_or_none.return_value = "run-1" if run_exists else None

    # traces result
    traces_result = MagicMock()
    traces_result.scalars.return_value = iter(traces)

    session.execute.side_effect = [run_check, traces_result]
    return session


def make_session_for_single_trace(trace):
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = trace
    session.execute.return_value = result
    return session


@asynccontextmanager
async def make_db_ctx(session):
    yield session


# ── Tests: list traces ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_traces_returns_traces():
    trace = make_trace()
    session = make_session_with_run_and_traces(run_exists=True, traces=[trace])

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.api.routes.traces.get_db", lambda: make_db_ctx(session)),
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/runs/run-1/trace")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == "trace-1"
    assert data[0]["trace_type"] == "reflection"
    assert data[0]["agent_role"] == "builder"
    assert data[0]["content"] == "I am thinking about this task carefully."


@pytest.mark.asyncio
async def test_list_traces_returns_empty_list_when_none():
    session = make_session_with_run_and_traces(run_exists=True, traces=[])

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.api.routes.traces.get_db", lambda: make_db_ctx(session)),
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/runs/run-1/trace")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_traces_404_when_run_not_found():
    session = make_session_with_run_and_traces(run_exists=False, traces=[])

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.api.routes.traces.get_db", lambda: make_db_ctx(session)),
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/runs/nonexistent/trace")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_traces_all_fields_present():
    trace = make_trace(
        trace_id="tr-abc",
        task_id="task-99",
        agent_role="reviewer",
        trace_type="self_check",
        content="Check passed.",
        context={"files": ["auth.py"]},
        tokens_used=120,
    )
    session = make_session_with_run_and_traces(run_exists=True, traces=[trace])

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.api.routes.traces.get_db", lambda: make_db_ctx(session)),
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/runs/run-1/trace")

    assert response.status_code == 200
    item = response.json()[0]
    assert item["id"] == "tr-abc"
    assert item["task_id"] == "task-99"
    assert item["agent_role"] == "reviewer"
    assert item["trace_type"] == "self_check"
    assert item["context"] == {"files": ["auth.py"]}
    assert item["tokens_used"] == 120
    assert "created_at" in item


# ── Tests: get single trace ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trace_by_id_returns_trace():
    trace = make_trace(trace_id="trace-xyz", trace_type="decision")
    session = make_session_for_single_trace(trace)

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.api.routes.traces.get_db", lambda: make_db_ctx(session)),
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/runs/run-1/trace/trace-xyz")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "trace-xyz"
    assert data["trace_type"] == "decision"


@pytest.mark.asyncio
async def test_get_trace_404_when_not_found():
    session = make_session_for_single_trace(None)

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.api.routes.traces.get_db", lambda: make_db_ctx(session)),
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/runs/run-1/trace/nonexistent")

    assert response.status_code == 404


# ── Tests: TraceOut schema ─────────────────────────────────────────────────────


class TestTraceOut:
    def test_from_orm(self):
        from phalanx.api.routes.traces import TraceOut

        trace = make_trace()
        out = TraceOut.from_orm(trace)

        assert out.id == "trace-1"
        assert out.run_id == "run-1"
        assert out.task_id == "task-1"
        assert out.agent_role == "builder"
        assert out.trace_type == "reflection"
        assert out.content == "I am thinking about this task carefully."
        assert out.context == {}
        assert out.tokens_used == 50
        assert "2026" in out.created_at

    def test_from_orm_null_task_id(self):
        from phalanx.api.routes.traces import TraceOut

        trace = make_trace(task_id=None)
        out = TraceOut.from_orm(trace)
        assert out.task_id is None

    def test_from_orm_null_context_defaults_to_empty_dict(self):
        from phalanx.api.routes.traces import TraceOut

        trace = make_trace()
        trace.context = None
        out = TraceOut.from_orm(trace)
        assert out.context == {}
