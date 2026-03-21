"""
Unit tests for phalanx/agents/tech_lead.py — Phase 2D.

All Anthropic API calls and DB session calls are mocked.

Tests cover:
  - Happy path: tasks + deps written, output dict correct
  - Empty epics → AgentResult(success=False)
  - Claude API failure → AgentResult(success=False)
  - JSON parse failure → AgentResult(success=False)
  - Empty tasks in response → AgentResult(success=False)
  - TaskDependency rows written correctly
  - Unknown dep sequence skipped (no crash)
  - Critical path computed via DagResolver
  - DagResolver exception falls back to sum of minutes
  - Markdown fenced response parsed correctly
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.tech_lead import TechLeadAgent


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_work_order(title="Build real-estate portal"):
    wo = MagicMock()
    wo.title = title
    wo.description = "Listings, search, auth"
    return wo


def _make_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _make_epics(n=2):
    return [
        {"id": f"epic-{i}", "title": f"Epic {i}", "description": f"Desc {i}",
         "sequence_num": i, "estimated_minutes": 30}
        for i in range(n)
    ]


def _valid_response(tasks=None):
    data = {
        "api_contract": {
            "endpoints": [{"method": "GET", "path": "/api/listings", "description": "List"}]
        },
        "db_schema": {
            "tables": [{"name": "listings", "columns": ["id", "title", "price"]}]
        },
        "tasks": tasks or [
            {
                "epic_index": 0,
                "title": "Scaffold infrastructure",
                "agent_role": "builder",
                "sequence_num": 1,
                "estimated_minutes": 30,
                "files_likely_touched": ["models.py"],
                "dependencies": [],
            },
            {
                "epic_index": 1,
                "title": "Build listings API",
                "agent_role": "builder",
                "sequence_num": 2,
                "estimated_minutes": 45,
                "files_likely_touched": ["api/listings.py"],
                "dependencies": [{"depends_on_seq": 1, "dep_type": "full"}],
            },
        ],
    }
    return json.dumps(data)


def _make_agent():
    agent = TechLeadAgent.__new__(TechLeadAgent)
    agent.run_id = "run-001"
    agent.task_id = None
    agent.agent_id = "tech_lead"
    agent.token_budget = 100000
    agent._tokens_used = 0
    import structlog
    agent._log = structlog.get_logger("test").bind(run_id="run-001")
    agent._settings = MagicMock()
    agent._settings.anthropic_model_default = "claude-sonnet-4-6"
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestTechLeadAgent:
    async def test_happy_path_returns_tasks_and_artifacts(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is True
        # tech_lead appends integration_wiring + verifier tasks after builders → 4 total
        assert len(result.output["tasks"]) == 4
        assert "api_contract" in result.output
        assert "db_schema" in result.output
        assert result.output["critical_path_minutes"] > 0

    async def test_task_rows_added_to_session(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is True
        # 4 tasks (2 builders + integration_wiring + verifier) + 4 deps (1+2+1)
        assert session.add.call_count == 8
        # tech_lead commits twice: once for LLM tasks, once for injected post-build tasks
        assert session.commit.await_count == 2

    async def test_dependency_row_written(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            result = await agent.execute_for_run(session, wo, pm_output)

        tasks = result.output["tasks"]
        # task seq=2 depends on seq=1
        deps = tasks[1]["dependencies"]
        assert len(deps) == 1
        assert deps[0]["dep_type"] == "full"

    async def test_empty_epics_returns_error(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": [], "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is False
        assert "no epics" in result.error
        session.add.assert_not_called()

    async def test_claude_api_failure_returns_error(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", side_effect=RuntimeError("Rate limited")):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is False
        assert "Rate limited" in result.error
        session.add.assert_not_called()

    async def test_invalid_json_returns_parse_error(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value="not json"):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is False
        assert "JSON parse error" in result.error

    async def test_empty_tasks_in_response_returns_error(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        response = json.dumps({"api_contract": {}, "db_schema": {}, "tasks": []})
        with patch.object(agent, "_call_claude", return_value=response):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is False
        assert "no tasks" in result.error

    async def test_unknown_dep_seq_skipped_no_crash(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        tasks = [
            {
                "epic_index": 0,
                "title": "Task A",
                "agent_role": "builder",
                "sequence_num": 1,
                "estimated_minutes": 20,
                "files_likely_touched": [],
                "dependencies": [{"depends_on_seq": 999, "dep_type": "full"}],  # unknown
            }
        ]
        response = json.dumps({"api_contract": {}, "db_schema": {}, "tasks": tasks})
        with patch.object(agent, "_call_claude", return_value=response):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is True
        # 1 builder + integration_wiring + verifier injected = 3 tasks
        assert len(result.output["tasks"]) == 3
        assert result.output["tasks"][0]["dependencies"] == []

    async def test_critical_path_computed(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        # Linear chain: 30 + 45 + 3 (wiring) + 5 (verifier) = 83 critical path
        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.output["critical_path_minutes"] == 83

    async def test_dag_resolver_exception_falls_back_to_sum(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            with patch("phalanx.agents.tech_lead.DagResolver.resolve", side_effect=Exception("cycle!")):
                result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is True
        # fallback = sum of all task estimated_minutes = 30 + 45 + 3 + 5 = 83
        assert result.output["critical_path_minutes"] == 83

    async def test_markdown_fenced_response_parsed(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        pm_output = {"epics": _make_epics(2), "app_type": "web"}

        fenced = f"```json\n{_valid_response()}\n```"
        with patch.object(agent, "_call_claude", return_value=fenced):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.success is True
        # tech_lead appends integration_wiring + verifier tasks after builders → 4 total
        assert len(result.output["tasks"]) == 4

    async def test_epic_id_assigned_to_tasks(self):
        agent = _make_agent()
        session = _make_session()
        wo = _make_work_order()
        epics = _make_epics(2)
        pm_output = {"epics": epics, "app_type": "web"}

        with patch.object(agent, "_call_claude", return_value=_valid_response()):
            result = await agent.execute_for_run(session, wo, pm_output)

        assert result.output["tasks"][0]["epic_id"] == epics[0]["id"]
        assert result.output["tasks"][1]["epic_id"] == epics[1]["id"]

    def test_execute_raises_not_implemented(self):
        agent = _make_agent()
        import asyncio
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(agent.execute())
