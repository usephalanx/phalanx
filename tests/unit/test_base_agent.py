"""
Unit tests for forge/agents/base.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.agents.base import AgentResult, BaseAgent, get_anthropic_client

# ── Concrete subclass for testing ─────────────────────────────────────────────


class ConcreteAgent(BaseAgent):
    AGENT_ROLE = "test_agent"

    async def execute(self) -> AgentResult:
        return AgentResult(success=True, output={"done": True})


# ── AgentResult ────────────────────────────────────────────────────────────────


class TestAgentResult:
    def test_success_result(self):
        r = AgentResult(success=True, output={"key": "value"})
        assert r.success is True
        assert r.output == {"key": "value"}
        assert r.tokens_used == 0
        assert r.error is None

    def test_failure_result(self):
        r = AgentResult(success=False, output={}, error="Something broke")
        assert r.success is False
        assert r.error == "Something broke"

    def test_tokens_used_tracked(self):
        r = AgentResult(success=True, output={}, tokens_used=1500)
        assert r.tokens_used == 1500

    def test_repr(self):
        r = AgentResult(success=True, output={}, tokens_used=500)
        text = repr(r)
        assert "AgentResult" in text
        assert "True" in text
        assert "500" in text


# ── BaseAgent init ─────────────────────────────────────────────────────────────


class TestBaseAgentInit:
    def test_basic_construction(self):
        agent = ConcreteAgent(run_id="run-1", agent_id="tester")
        assert agent.run_id == "run-1"
        assert agent.agent_id == "tester"
        assert agent.task_id is None
        assert agent._tokens_used == 0

    def test_uuid_run_id_converted_to_str(self):
        import uuid

        uid = uuid.uuid4()
        agent = ConcreteAgent(run_id=uid, agent_id="tester")
        assert isinstance(agent.run_id, str)
        assert agent.run_id == str(uid)

    def test_task_id_stored(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", task_id="t1")
        assert agent.task_id == "t1"

    def test_token_budget_default(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester")
        assert agent.token_budget > 0

    def test_custom_token_budget(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=5000)
        assert agent.token_budget == 5000

    def test_log_is_bound_with_context(self):
        agent = ConcreteAgent(run_id="r1", agent_id="test-agent", task_id="t1")
        # Logger should exist and be bound
        assert agent._log is not None


# ── Token budget ───────────────────────────────────────────────────────────────


class TestTokenBudget:
    def test_check_budget_passes_within_limit(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=1000)
        agent._tokens_used = 500
        agent._check_budget(400)  # 500 + 400 = 900 < 1000 — OK

    def test_check_budget_raises_when_exceeded(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=1000)
        agent._tokens_used = 800
        with pytest.raises(RuntimeError, match="Token budget exceeded"):
            agent._check_budget(300)  # 800 + 300 = 1100 > 1000

    def test_check_budget_at_exact_limit_passes(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=1000)
        agent._tokens_used = 0
        agent._check_budget(1000)  # exactly at limit — OK


# ── _call_claude ───────────────────────────────────────────────────────────────


class TestCallClaude:
    def test_call_claude_returns_text(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello from Claude")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-sonnet-4-6"

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("forge.agents.base.get_anthropic_client", return_value=mock_client):
            result = agent._call_claude(
                messages=[{"role": "user", "content": "Hi"}],
                system="You are helpful",
            )

        assert result == "Hello from Claude"

    def test_call_claude_tracks_tokens(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Response")]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.model = "claude-sonnet-4-6"

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("forge.agents.base.get_anthropic_client", return_value=mock_client):
            agent._call_claude(messages=[{"role": "user", "content": "Test"}])

        assert agent._tokens_used == 150

    def test_call_claude_respects_budget(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100)
        agent._tokens_used = 90

        with pytest.raises(RuntimeError, match="Token budget exceeded"):
            agent._call_claude(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=200,
            )


# ── _audit ─────────────────────────────────────────────────────────────────────


class TestAudit:
    async def test_audit_writes_to_db(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester")

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("forge.db.session.get_db", mock_get_db):
            await agent._audit(
                event_type="test_event",
                payload={"key": "value"},
            )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    async def test_audit_is_non_fatal_on_db_error(self):
        """Audit failures must NOT raise — they should just log a warning."""
        agent = ConcreteAgent(run_id="r1", agent_id="tester")

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def failing_get_db():
            raise RuntimeError("DB unavailable")
            yield  # type: ignore[misc]

        with patch("forge.db.session.get_db", failing_get_db):
            # Should NOT raise
            await agent._audit(event_type="test_event")


# ── _transition_run ────────────────────────────────────────────────────────────


class TestTransitionRun:
    async def test_valid_transition_succeeds(self):
        agent = ConcreteAgent(run_id="r1", agent_id="tester")

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with (
            patch("forge.db.session.get_db", mock_get_db),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            # INTAKE → RESEARCHING is a valid transition
            await agent._transition_run("INTAKE", "RESEARCHING")

        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    async def test_invalid_transition_raises(self):
        from forge.workflow.state_machine import InvalidTransitionError

        agent = ConcreteAgent(run_id="r1", agent_id="tester")

        # RESEARCHING → INTAKE is an invalid non-terminal transition
        with pytest.raises(InvalidTransitionError):
            await agent._transition_run("RESEARCHING", "INTAKE")


# ── get_anthropic_client singleton ────────────────────────────────────────────


class TestGetAnthropicClient:
    def test_returns_same_instance(self):
        import forge.agents.base as base_module

        # Reset singleton
        base_module._anthropic_client = None
        with patch("forge.agents.base.Anthropic") as mock_anthropic:  # noqa: N806
            mock_anthropic.return_value = MagicMock()
            c1 = get_anthropic_client()
            c2 = get_anthropic_client()
        assert c1 is c2
