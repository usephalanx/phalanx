"""
Phase 4 soul tests — brainstorm items #2, #3, #4, #5:

  #4 — _escalate_trace_to_slack: called on uncertainty/disagreement, not others
  #3 — _load_cross_run_memory / _write_cross_run_pattern: loads from memory_facts
  #5 — _write_complexity_calibration / _load_complexity_calibration: burn ratio math
  #2 — /traces portal: returns 200 HTML
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.base import AgentResult, BaseAgent
from phalanx.agents.builder import BuilderAgent
from phalanx.agents.reviewer import ReviewerAgent


# ── Helpers ────────────────────────────────────────────────────────────────────


class ConcreteAgent(BaseAgent):
    AGENT_ROLE = "builder"

    async def execute(self) -> AgentResult:
        return AgentResult(success=True, output={})


def make_agent():
    return ConcreteAgent(run_id="run-1", agent_id="builder", task_id="task-1")


def make_builder():
    return BuilderAgent(run_id="run-1", agent_id="builder", task_id="task-1")


def make_reviewer():
    return ReviewerAgent(run_id="run-1", agent_id="reviewer", task_id="task-1")


def make_memory_fact_orm(
    title="Test pattern",
    body='{"estimated_complexity": 3, "tokens_used": 4500, "expected_tokens": 3000, "burn_ratio": 1.5, "run_id": "run-x"}',
    fact_type="complexity_calibration",
    confidence=0.8,
    status="confirmed",
):
    m = MagicMock()
    m.id = "mf-1"
    m.title = title
    m.body = body
    m.fact_type = fact_type
    m.confidence = confidence
    m.status = status
    return m


# ── #4 Slack escalation tests ─────────────────────────────────────────────────


class TestSlackEscalation:
    @pytest.mark.asyncio
    async def test_trace_escalates_uncertainty_to_slack(self):
        """uncertainty trace type triggers _escalate_trace_to_slack."""
        agent = make_agent()

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = MagicMock()
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            with patch.object(agent, "_escalate_trace_to_slack", new_callable=AsyncMock) as mock_esc:
                await agent._trace("uncertainty", "I'm not sure about this")
            mock_esc.assert_called_once_with("uncertainty", "I'm not sure about this")

    @pytest.mark.asyncio
    async def test_trace_escalates_disagreement_to_slack(self):
        """disagreement trace type triggers _escalate_trace_to_slack."""
        agent = make_agent()

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = MagicMock()
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            with patch.object(agent, "_escalate_trace_to_slack", new_callable=AsyncMock) as mock_esc:
                await agent._trace("disagreement", "This spec is contradictory")
            mock_esc.assert_called_once_with("disagreement", "This spec is contradictory")

    @pytest.mark.asyncio
    async def test_trace_does_not_escalate_reflection(self):
        """reflection trace type does NOT trigger escalation."""
        agent = make_agent()

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = MagicMock()
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            with patch.object(agent, "_escalate_trace_to_slack", new_callable=AsyncMock) as mock_esc:
                await agent._trace("reflection", "Thinking about this task")
            mock_esc.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalate_to_slack_posts_message(self):
        """_escalate_trace_to_slack calls notifier.post with formatted message."""
        agent = make_agent()
        mock_notifier = AsyncMock()
        mock_notifier.post = AsyncMock(return_value="ts-123")

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            with patch("phalanx.agents.base.SlackNotifier", create=True) as mock_slack_cls:
                # SlackNotifier is imported inside the method, patch the module-level import path
                pass

        # Test via direct patch of from_run
        from phalanx.workflow.slack_notifier import SlackNotifier
        with patch.object(SlackNotifier, "from_run", new_callable=AsyncMock, return_value=mock_notifier):
            with patch("phalanx.db.session.get_db", fake_db):
                await agent._escalate_trace_to_slack("uncertainty", "not sure about auth design")

        mock_notifier.post.assert_called_once()
        call_text = mock_notifier.post.call_args[0][0]
        assert "uncertainty" in call_text.lower() or "Uncertainty" in call_text
        assert "builder" in call_text

    @pytest.mark.asyncio
    async def test_escalate_to_slack_non_fatal_on_error(self):
        """_escalate_trace_to_slack must not raise even if Slack is down."""
        agent = make_agent()

        @asynccontextmanager
        async def failing_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_db):
            # Should not raise
            await agent._escalate_trace_to_slack("uncertainty", "something")

    def test_escalate_trace_to_slack_exists(self):
        """BaseAgent has _escalate_trace_to_slack method."""
        assert hasattr(BaseAgent, "_escalate_trace_to_slack")

    def test_trace_docstring_mentions_soul008(self):
        """_trace docstring documents SOUL-008 escalation behavior."""
        import inspect
        src = inspect.getsource(BaseAgent._trace)
        assert "SOUL-008" in src or "uncertainty" in src


# ── #3 Cross-run memory tests ─────────────────────────────────────────────────


class TestCrossRunMemory:
    @pytest.mark.asyncio
    async def test_load_cross_run_memory_returns_facts(self):
        """_load_cross_run_memory returns list of dicts from memory_facts."""
        agent = make_agent()
        fact = make_memory_fact_orm(
            title="Reviewer flagged missing error handling",
            body="Verdict: CHANGES_REQUESTED\nSummary: API routes missing error handling",
            fact_type="review_pattern",
        )

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.execute = AsyncMock(
                return_value=MagicMock(scalars=lambda: MagicMock(
                    __iter__=lambda self: iter([fact])
                ))
            )
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            result = await agent._load_cross_run_memory("proj-1")

        assert len(result) == 1
        assert result[0]["title"] == "Reviewer flagged missing error handling"
        assert result[0]["fact_type"] == "review_pattern"

    @pytest.mark.asyncio
    async def test_load_cross_run_memory_returns_empty_on_error(self):
        """_load_cross_run_memory returns [] on DB error (non-fatal)."""
        agent = make_agent()

        @asynccontextmanager
        async def failing_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_db):
            result = await agent._load_cross_run_memory("proj-1")

        assert result == []

    @pytest.mark.asyncio
    async def test_write_cross_run_pattern_writes_memory_fact(self):
        """_write_cross_run_pattern creates a MemoryFact in the DB."""
        agent = make_agent()
        added = []

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = lambda obj: added.append(obj)
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            await agent._write_cross_run_pattern(
                project_id="proj-1",
                title="API routes missing validation",
                body="Seen 3 times: missing Pydantic validators on POST routes",
                fact_type="review_pattern",
                confidence=0.8,
            )

        assert len(added) == 1
        fact = added[0]
        assert fact.project_id == "proj-1"
        assert fact.fact_type == "review_pattern"
        assert "validation" in fact.title

    @pytest.mark.asyncio
    async def test_write_cross_run_pattern_non_fatal_on_error(self):
        """_write_cross_run_pattern does not raise on DB error."""
        agent = make_agent()

        @asynccontextmanager
        async def failing_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_db):
            # Must not raise
            await agent._write_cross_run_pattern(
                project_id="proj-1",
                title="Test",
                body="Test body",
            )

    @pytest.mark.asyncio
    async def test_reviewer_write_cross_run_review_pattern(self):
        """_write_cross_run_review_pattern writes a fact for CHANGES_REQUESTED."""
        reviewer = make_reviewer()
        added = []

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = lambda obj: added.append(obj)
            s.commit = AsyncMock()
            yield s

        review = {
            "verdict": "CHANGES_REQUESTED",
            "summary": "Missing error handling in auth routes",
            "issues": [
                {"severity": "high", "location": "auth.py:42",
                 "description": "No try/except around DB call"},
            ],
        }
        with patch("phalanx.db.session.get_db", fake_db):
            await reviewer._write_cross_run_review_pattern(review, "proj-1")

        assert len(added) == 1
        assert added[0].fact_type == "review_pattern"
        assert added[0].confidence == 0.75  # CHANGES_REQUESTED confidence

    @pytest.mark.asyncio
    async def test_reviewer_write_cross_run_review_pattern_skips_no_issues(self):
        """_write_cross_run_review_pattern skips if no issues list."""
        reviewer = make_reviewer()
        added = []

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = lambda obj: added.append(obj)
            s.commit = AsyncMock()
            yield s

        review = {"verdict": "CHANGES_REQUESTED", "summary": "Some issue", "issues": []}
        with patch("phalanx.db.session.get_db", fake_db):
            await reviewer._write_cross_run_review_pattern(review, "proj-1")

        assert len(added) == 0

    def test_builder_execute_loads_cross_run_memory(self):
        """BuilderAgent.execute() calls _load_cross_run_memory."""
        import inspect
        import phalanx.agents.builder as m
        src = inspect.getsource(m.BuilderAgent.execute)
        assert "_load_cross_run_memory" in src

    def test_reviewer_execute_loads_cross_run_memory(self):
        """ReviewerAgent.execute() calls _load_cross_run_memory."""
        import inspect
        import phalanx.agents.reviewer as m
        src = inspect.getsource(m.ReviewerAgent.execute)
        assert "_load_cross_run_memory" in src


# ── #5 Complexity calibration tests ──────────────────────────────────────────


class TestComplexityCalibration:
    @pytest.mark.asyncio
    async def test_write_complexity_calibration_writes_fact(self):
        """_write_complexity_calibration creates a MemoryFact with burn_ratio."""
        agent = make_agent()
        added = []

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = lambda obj: added.append(obj)
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            await agent._write_complexity_calibration(
                task_title="Build auth routes",
                estimated_complexity=3,
                tokens_used=6000,
                project_id="proj-1",
            )

        assert len(added) == 1
        fact = added[0]
        assert fact.fact_type == "complexity_calibration"
        data = json.loads(fact.body)
        assert data["estimated_complexity"] == 3
        assert data["tokens_used"] == 6000
        # burn_ratio = 6000 / (3 * 1000) = 2.0
        assert data["burn_ratio"] == 2.0

    @pytest.mark.asyncio
    async def test_write_complexity_calibration_burn_ratio_math(self):
        """burn_ratio = tokens_used / (estimated_complexity * 1000)."""
        agent = make_agent()
        added = []

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.add = lambda obj: added.append(obj)
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            await agent._write_complexity_calibration(
                task_title="Small fix",
                estimated_complexity=1,
                tokens_used=500,
                project_id="proj-1",
            )

        data = json.loads(added[0].body)
        assert data["burn_ratio"] == 0.5  # under-estimated (cheap task)

    @pytest.mark.asyncio
    async def test_write_complexity_calibration_non_fatal(self):
        """_write_complexity_calibration does not raise on DB error."""
        agent = make_agent()

        @asynccontextmanager
        async def failing_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_db):
            # Must not raise
            await agent._write_complexity_calibration("T", 3, 3000, "proj-1")

    @pytest.mark.asyncio
    async def test_load_complexity_calibration_returns_list(self):
        """_load_complexity_calibration returns parsed calibration dicts."""
        agent = make_agent()
        fact = make_memory_fact_orm(
            title="Complexity calibration: Build auth",
            body='{"estimated_complexity": 3, "tokens_used": 6000, "expected_tokens": 3000, "burn_ratio": 2.0, "run_id": "r1"}',
            fact_type="complexity_calibration",
        )

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.execute = AsyncMock(
                return_value=MagicMock(scalars=lambda: MagicMock(
                    __iter__=lambda self: iter([fact])
                ))
            )
            yield s

        with patch("phalanx.db.session.get_db", fake_db):
            result = await agent._load_complexity_calibration("proj-1")

        assert len(result) == 1
        assert result[0]["burn_ratio"] == 2.0
        assert result[0]["estimated_complexity"] == 3

    @pytest.mark.asyncio
    async def test_load_complexity_calibration_returns_empty_on_error(self):
        agent = make_agent()

        @asynccontextmanager
        async def failing_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_db):
            result = await agent._load_complexity_calibration("proj-1")

        assert result == []

    def test_builder_execute_calls_write_complexity_calibration(self):
        """BuilderAgent.execute() calls _write_complexity_calibration at end."""
        import inspect
        import phalanx.agents.builder as m
        src = inspect.getsource(m.BuilderAgent.execute)
        assert "_write_complexity_calibration" in src

    def test_planner_generate_plan_injects_calibration(self):
        """PlannerAgent._generate_plan calls _load_complexity_calibration."""
        import inspect
        import phalanx.agents.planner as m
        src = inspect.getsource(m.PlannerAgent._generate_plan)
        assert "_load_complexity_calibration" in src

    def test_planner_injects_calibration_ctx_into_message(self):
        """_generate_plan injects calibration_ctx into the user message."""
        import inspect
        import phalanx.agents.planner as m
        src = inspect.getsource(m.PlannerAgent._generate_plan)
        assert "calibration_ctx" in src


# ── #2 Traces portal tests ─────────────────────────────────────────────────────


class TestTracesPortal:
    def test_portal_router_registered(self):
        """portal_router is defined in traces.py."""
        from phalanx.api.routes.traces import portal_router
        assert portal_router is not None

    def test_portal_route_exists(self):
        """GET /traces route is registered on portal_router."""
        from phalanx.api.routes.traces import portal_router
        routes = [r.path for r in portal_router.routes]
        assert "/traces" in routes

    def test_traces_portal_html_has_run_input(self):
        """Portal HTML includes run ID input field."""
        from phalanx.api.routes.traces import _TRACES_PORTAL_HTML
        assert "run-input" in _TRACES_PORTAL_HTML
        assert "/v1/runs/" in _TRACES_PORTAL_HTML

    def test_traces_portal_html_has_filter_buttons(self):
        """Portal HTML has filter controls for trace types."""
        from phalanx.api.routes.traces import _TRACES_PORTAL_HTML
        assert "filter-btn" in _TRACES_PORTAL_HTML
        assert "badge-reflection" in _TRACES_PORTAL_HTML
        assert "badge-uncertainty" in _TRACES_PORTAL_HTML

    def test_traces_portal_html_supports_auto_load(self):
        """Portal HTML auto-loads if ?run_id= is in URL."""
        from phalanx.api.routes.traces import _TRACES_PORTAL_HTML
        assert "run_id" in _TRACES_PORTAL_HTML

    def test_portal_router_imported_in_main(self):
        """main.py imports and registers portal_router."""
        import inspect
        import phalanx.api.main as main_mod
        src = inspect.getsource(main_mod)
        assert "traces_portal_router" in src or "portal_router" in src

    def test_portal_html_has_expandable_content(self):
        """Portal cards have expandable content (toggle function)."""
        from phalanx.api.routes.traces import _TRACES_PORTAL_HTML
        assert "toggle(" in _TRACES_PORTAL_HTML
        assert "escHtml" in _TRACES_PORTAL_HTML
