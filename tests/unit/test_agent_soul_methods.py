"""
Unit tests for BaseAgent soul methods: _trace(), _reflect(), _decide().
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.base import AgentResult, BaseAgent


class ConcreteAgent(BaseAgent):
    AGENT_ROLE = "builder"

    async def execute(self) -> AgentResult:
        return AgentResult(success=True, output={})


def make_agent(**kwargs):
    return ConcreteAgent(run_id="run-1", agent_id="builder", task_id="task-1", **kwargs)


# ── _trace ─────────────────────────────────────────────────────────────────────


class TestTrace:
    @pytest.mark.asyncio
    async def test_trace_writes_to_db(self):
        agent = make_agent()

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("phalanx.db.session.get_db", mock_get_db):
            await agent._trace("reflection", "I am thinking about this task", {"key": "val"})

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trace_is_non_fatal_on_db_error(self):
        """_trace() must not raise even if the DB write fails."""
        agent = make_agent()

        @asynccontextmanager
        async def failing_get_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_get_db):
            # Must not raise
            await agent._trace("decision", "chose approach A")

    @pytest.mark.asyncio
    async def test_trace_content_truncated_to_10000(self):
        """Content is truncated to 10,000 chars to guard against runaway reflections."""
        agent = make_agent()
        long_content = "x" * 50_000

        captured: list = []
        mock_session = AsyncMock()

        def _capture_add(obj):
            captured.append(obj)

        mock_session.add = _capture_add
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("phalanx.db.session.get_db", mock_get_db):
            await agent._trace("reflection", long_content)

        assert len(captured) == 1
        assert len(captured[0].content) <= 10_000

    @pytest.mark.asyncio
    async def test_trace_sets_correct_agent_fields(self):
        agent = make_agent()
        captured: list = []

        mock_session = AsyncMock()
        mock_session.add = lambda obj: captured.append(obj)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("phalanx.db.session.get_db", mock_get_db):
            await agent._trace("self_check", "looks good", {"files": ["auth.py"]})

        trace = captured[0]
        assert trace.run_id == "run-1"
        assert trace.task_id == "task-1"
        assert trace.agent_role == "builder"
        assert trace.agent_id == "builder"
        assert trace.trace_type == "self_check"
        assert trace.context == {"files": ["auth.py"]}


# ── _reflect ────────────────────────────────────────────────────────────────────


class TestReflect:
    def _mock_claude(self, text: str) -> MagicMock:
        mock_client = MagicMock()
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        resp.usage.input_tokens = 50
        resp.usage.output_tokens = 30
        resp.model = "claude-sonnet-4-6"
        mock_client.messages.create.return_value = resp
        return mock_client

    def test_reflect_returns_text(self):
        agent = make_agent(token_budget=100_000)
        mock_client = self._mock_claude("I see this is a straightforward task.")

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            result = agent._reflect(
                task_description="Add login page",
                context="Plan: use React",
            )

        assert "straightforward" in result

    def test_reflect_returns_empty_on_failure(self):
        agent = make_agent(token_budget=100_000)

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("API down")

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            result = agent._reflect(task_description="Add login page")

        assert result == ""

    def test_reflect_uses_soul_as_system_prompt(self):
        agent = make_agent(token_budget=100_000)
        captured_calls: list = []

        mock_client = MagicMock()
        resp = MagicMock()
        resp.content = [MagicMock(text="reflection")]
        resp.usage.input_tokens = 10
        resp.usage.output_tokens = 5
        resp.model = "claude-sonnet-4-6"

        def _capture(**kwargs):
            captured_calls.append(kwargs)
            return resp

        mock_client.messages.create.side_effect = _capture

        soul = "You are an adversarial reviewer."
        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            agent._reflect(task_description="Review code", soul=soul)

        assert len(captured_calls) == 1
        assert captured_calls[0]["system"] == soul

    def test_reflect_uses_builder_template_for_builder_role(self):
        """Builder agent uses the BUILDER_REFLECTION_PROMPT template."""
        agent = make_agent(token_budget=100_000)
        captured_messages: list = []

        mock_client = MagicMock()
        resp = MagicMock()
        resp.content = [MagicMock(text="builder reflection")]
        resp.usage.input_tokens = 10
        resp.usage.output_tokens = 5
        resp.model = "claude-sonnet-4-6"

        def _capture(**kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return resp

        mock_client.messages.create.side_effect = _capture

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            agent._reflect(
                task_description="Add auth module",
                context="Plan: JWT tokens",
            )

        prompt = captured_messages[0]["content"]
        assert "Add auth module" in prompt  # task_description filled in

    def test_reflect_is_non_fatal_on_token_budget_exceeded(self):
        """If token budget is exhausted, _reflect() returns '' rather than raising."""
        agent = make_agent(token_budget=10)
        agent._tokens_used = 10  # budget exhausted

        result = agent._reflect(task_description="anything")
        assert result == ""


# ── _decide ─────────────────────────────────────────────────────────────────────


class TestDecide:
    def test_decide_does_not_raise(self):
        agent = make_agent()
        # Should log without raising
        agent._decide(
            decision="directory_layout",
            chosen="src/components/",
            alternatives=["components/", "app/"],
            rationale="Matches existing pattern",
        )

    def test_decide_minimal_args(self):
        agent = make_agent()
        agent._decide(decision="approach", chosen="JWT")

    def test_decide_with_no_alternatives(self):
        agent = make_agent()
        agent._decide(decision="db_driver", chosen="asyncpg", rationale="best async support")
