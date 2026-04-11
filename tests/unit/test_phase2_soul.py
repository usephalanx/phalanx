"""
Phase 2 soul tests:
  Track A — _load_episode_memory()
  Track B — _load_reviewer_feedback() + reflexion context injection
  Track C — _call_claude_with_thinking()
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.base import AgentResult, BaseAgent


# ── Shared helpers ─────────────────────────────────────────────────────────────


class ConcreteAgent(BaseAgent):
    AGENT_ROLE = "builder"

    async def execute(self) -> AgentResult:
        return AgentResult(success=True, output={})


def make_agent(**kwargs):
    return ConcreteAgent(run_id="run-1", agent_id="builder", task_id="task-3", **kwargs)


def make_trace_orm(
    trace_id="tr-1",
    run_id="run-1",
    task_id="task-1",
    agent_role="builder",
    trace_type="reflection",
    content="I reflected on this task.",
    context=None,
):
    t = MagicMock()
    t.id = trace_id
    t.run_id = run_id
    t.task_id = task_id
    t.agent_role = agent_role
    t.trace_type = trace_type
    t.content = content
    t.context = context or {}
    return t


def make_task_orm(
    task_id="t-rev-1",
    agent_role="reviewer",
    sequence_num=2,
    status="COMPLETED",
    output=None,
):
    t = MagicMock()
    t.id = task_id
    t.agent_role = agent_role
    t.sequence_num = sequence_num
    t.status = status
    t.output = output or {}
    return t


@asynccontextmanager
async def mock_db(session):
    yield session


# ══════════════════════════════════════════════════════════════════════════════
# Track A — _load_episode_memory()
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadEpisodeMemory:
    @pytest.mark.asyncio
    async def test_returns_list_of_trace_dicts(self):
        agent = make_agent()

        # DB returns newest-first (ORDER BY created_at DESC)
        trace_new = make_trace_orm(task_id="task-2", trace_type="self_check", content="Self-check B")
        trace_old = make_trace_orm(task_id="task-1", trace_type="reflection", content="Prior reflection A")

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value = [trace_new, trace_old]  # newest first from DB
        mock_session.execute.return_value = result_mock

        with patch("phalanx.db.session.get_db", lambda: mock_db(mock_session)):
            memory = await agent._load_episode_memory()

        # reversed() makes oldest first
        assert len(memory) == 2
        assert memory[0]["trace_type"] == "reflection"   # oldest
        assert "Prior reflection A" in memory[0]["content"]
        assert memory[1]["trace_type"] == "self_check"   # newest

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_db_error(self):
        agent = make_agent()

        @asynccontextmanager
        async def failing_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_db):
            memory = await agent._load_episode_memory()

        assert memory == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_prior_traces(self):
        agent = make_agent()

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value = []
        mock_session.execute.return_value = result_mock

        with patch("phalanx.db.session.get_db", lambda: mock_db(mock_session)):
            memory = await agent._load_episode_memory()

        assert memory == []

    @pytest.mark.asyncio
    async def test_content_truncated_to_800_chars(self):
        agent = make_agent()

        long_trace = make_trace_orm(content="x" * 5_000)

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value = [long_trace]
        mock_session.execute.return_value = result_mock

        with patch("phalanx.db.session.get_db", lambda: mock_db(mock_session)):
            memory = await agent._load_episode_memory()

        assert len(memory[0]["content"]) <= 800

    @pytest.mark.asyncio
    async def test_memory_is_oldest_first(self):
        """Memory is returned oldest-first (chronological reading order)."""
        agent = make_agent()

        # DB returns newest-first (ORDER BY created_at DESC)
        tr_new = make_trace_orm(task_id="task-2", content="Newer")
        tr_old = make_trace_orm(task_id="task-1", content="Older")

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value = [tr_new, tr_old]  # newest first from DB
        mock_session.execute.return_value = result_mock

        with patch("phalanx.db.session.get_db", lambda: mock_db(mock_session)):
            memory = await agent._load_episode_memory()

        # reversed() in _load_episode_memory puts oldest first
        assert memory[0]["content"] == "Older"
        assert memory[1]["content"] == "Newer"


# ══════════════════════════════════════════════════════════════════════════════
# Track B — _load_reviewer_feedback() + reflexion context
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadReviewerFeedback:
    def _make_builder(self):
        from phalanx.agents.builder import BuilderAgent

        return BuilderAgent(run_id="run-1", task_id="task-3", agent_id="builder")

    @pytest.mark.asyncio
    async def test_returns_feedback_for_changes_requested(self):
        agent = self._make_builder()

        reviewer_output = {
            "verdict": "CHANGES_REQUESTED",
            "summary": "Missing error handling",
            "issues": [{"severity": "high", "description": "No try/except", "suggestion": "Add try/except"}],
        }
        reviewer_task = make_task_orm(agent_role="reviewer", output=reviewer_output)

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = reviewer_task
        mock_session.execute.return_value = result_mock

        feedback = await agent._load_reviewer_feedback(mock_session, before_seq=3)

        assert feedback is not None
        assert feedback["verdict"] == "CHANGES_REQUESTED"
        assert "Missing error handling" in feedback["summary"]

    @pytest.mark.asyncio
    async def test_returns_feedback_for_critical_issues(self):
        agent = self._make_builder()

        reviewer_output = {"verdict": "CRITICAL_ISSUES", "summary": "SQL injection", "issues": []}
        reviewer_task = make_task_orm(output=reviewer_output)

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = reviewer_task
        mock_session.execute.return_value = result_mock

        feedback = await agent._load_reviewer_feedback(mock_session, before_seq=3)
        assert feedback["verdict"] == "CRITICAL_ISSUES"

    @pytest.mark.asyncio
    async def test_returns_none_for_approved(self):
        agent = self._make_builder()

        reviewer_output = {"verdict": "APPROVED", "summary": "Looks good"}
        reviewer_task = make_task_orm(output=reviewer_output)

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = reviewer_task
        mock_session.execute.return_value = result_mock

        feedback = await agent._load_reviewer_feedback(mock_session, before_seq=3)
        assert feedback is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_reviewer_task(self):
        agent = self._make_builder()

        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        feedback = await agent._load_reviewer_feedback(mock_session, before_seq=3)
        assert feedback is None


class TestBuildPromptWithReflexion:
    def _make_builder(self):
        from phalanx.agents.builder import BuilderAgent

        return BuilderAgent(run_id="run-1", task_id="task-1", agent_id="builder")

    def _make_task(self, complexity=1):
        t = MagicMock()
        t.title = "Add login"
        t.description = "Add JWT login"
        t.agent_role = "builder"
        t.role_context = None
        t.estimated_complexity = complexity
        return t

    def test_reviewer_feedback_injected_into_prompt(self):
        agent = self._make_builder()
        task = self._make_task()
        feedback = {
            "verdict": "CHANGES_REQUESTED",
            "summary": "Missing error handling",
            "issues": [
                {"severity": "high", "location": "auth.py:12", "description": "No try/except", "suggestion": "Wrap in try/except"}
            ],
        }

        _, messages = agent._build_prompt(task, {}, {}, reviewer_feedback=feedback)
        content = messages[0]["content"]

        assert "CHANGES_REQUESTED" in content
        assert "Missing error handling" in content
        assert "No try/except" in content

    def test_no_reflexion_section_when_no_feedback(self):
        agent = self._make_builder()
        task = self._make_task()

        _, messages = agent._build_prompt(task, {}, {}, reviewer_feedback=None)
        content = messages[0]["content"]

        assert "PRIOR REVIEW" not in content

    def test_must_address_issues_instruction_present(self):
        agent = self._make_builder()
        task = self._make_task()
        feedback = {
            "verdict": "CRITICAL_ISSUES",
            "summary": "Security hole",
            "issues": [{"severity": "critical", "location": "main.py:5", "description": "SQL injection", "suggestion": "Use parameterized queries"}],
        }

        _, messages = agent._build_prompt(task, {}, {}, reviewer_feedback=feedback)
        content = messages[0]["content"]
        assert "MUST address" in content


# ══════════════════════════════════════════════════════════════════════════════
# Track C — _call_claude_with_thinking()
# ══════════════════════════════════════════════════════════════════════════════


class TestCallClaudeWithThinking:
    def _make_thinking_response(self, thinking_text: str, output_text: str):
        thinking_block = MagicMock()
        thinking_block.type = "thinking"
        thinking_block.thinking = thinking_text

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = output_text

        response = MagicMock()
        response.content = [thinking_block, text_block]
        response.usage.input_tokens = 100
        response.usage.output_tokens = 200
        response.model = "claude-sonnet-4-6"
        return response

    def test_returns_text_and_thinking(self):
        agent = make_agent(token_budget=100_000)
        resp = self._make_thinking_response(
            thinking_text="Let me reason through this...",
            output_text='{"summary": "done"}',
        )
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp

        with patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client):
            text, thinking = agent._call_claude_with_thinking(
                messages=[{"role": "user", "content": "Build auth"}],
                system="You are a senior engineer.",
            )

        assert text == '{"summary": "done"}'
        assert "reason through" in thinking

    def test_passes_thinking_param_to_api(self):
        agent = make_agent(token_budget=100_000)
        resp = self._make_thinking_response("thoughts", "output")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp

        with patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client):
            agent._call_claude_with_thinking(
                messages=[{"role": "user", "content": "task"}],
                budget_tokens=8_000,
                max_tokens=20_000,
            )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["thinking"]["type"] == "enabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 8_000
        assert call_kwargs["max_tokens"] == 20_000

    def test_returns_empty_thinking_when_no_thinking_block(self):
        agent = make_agent(token_budget=100_000)

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "output only"

        resp = MagicMock()
        resp.content = [text_block]
        resp.usage.input_tokens = 50
        resp.usage.output_tokens = 30
        resp.model = "claude-sonnet-4-6"

        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp

        with patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client):
            text, thinking = agent._call_claude_with_thinking(
                messages=[{"role": "user", "content": "task"}]
            )

        assert text == "output only"
        assert thinking == ""

    def test_tracks_tokens(self):
        agent = make_agent(token_budget=100_000)
        resp = self._make_thinking_response("thoughts", "output")
        resp.usage.input_tokens = 80
        resp.usage.output_tokens = 40
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp

        with patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client):
            agent._call_claude_with_thinking(messages=[{"role": "user", "content": "x"}])

        assert agent._tokens_used == 120

    def test_raises_budget_exceeded(self):
        agent = make_agent(token_budget=100)
        agent._tokens_used = 90

        with pytest.raises(RuntimeError, match="Token budget exceeded"):
            agent._call_claude_with_thinking(
                messages=[{"role": "user", "content": "x"}],
                max_tokens=200,
            )


# ══════════════════════════════════════════════════════════════════════════════
# Track C — Extended thinking wired into _generate_changes_blocking()
# ══════════════════════════════════════════════════════════════════════════════


class TestGenerateChangesBlockingWithThinking:
    def _make_builder(self):
        from phalanx.agents.builder import BuilderAgent

        return BuilderAgent(run_id="run-1", task_id="task-1", agent_id="builder", token_budget=200_000)

    def _make_task(self, complexity=5):
        t = MagicMock()
        t.title = "Refactor auth"
        t.description = "Major refactor"
        t.agent_role = "builder"
        t.role_context = None
        t.estimated_complexity = complexity
        return t

    @pytest.mark.asyncio
    async def test_uses_thinking_for_high_complexity(self):
        agent = self._make_builder()
        task = self._make_task(complexity=5)

        changes = {"summary": "done", "commit_message": "feat: x", "files": []}
        thinking_text = "Step 1: analyze the architecture..."

        with (
            patch.object(
                agent,
                "_call_claude_with_thinking",
                return_value=(json.dumps(changes), thinking_text),
            ) as mock_thinking,
            patch.object(agent, "_call_claude") as mock_regular,
            patch.object(agent, "_trace", AsyncMock()),
        ):
            result = await agent._generate_changes_blocking(task, {}, {}, MagicMock(), complexity=5)

        mock_thinking.assert_called_once()
        mock_regular.assert_not_called()
        assert result["summary"] == "done"

    @pytest.mark.asyncio
    async def test_persists_thinking_as_decision_trace(self):
        agent = self._make_builder()
        task = self._make_task(complexity=5)

        changes = {"summary": "done", "commit_message": "feat: x", "files": []}
        thinking_text = "I decided to use a factory pattern because..."

        trace_calls: list = []

        async def _capture_trace(trace_type, content, context=None):
            trace_calls.append((trace_type, content, context))

        with (
            patch.object(agent, "_call_claude_with_thinking", return_value=(json.dumps(changes), thinking_text)),
            patch.object(agent, "_trace", side_effect=_capture_trace),
        ):
            await agent._generate_changes_blocking(task, {}, {}, MagicMock(), complexity=5)

        assert any(t[0] == "decision" and "factory pattern" in t[1] for t in trace_calls)

    @pytest.mark.asyncio
    async def test_does_not_use_thinking_for_low_complexity(self):
        agent = self._make_builder()
        task = self._make_task(complexity=2)

        changes = {"summary": "done", "commit_message": "feat: x", "files": []}

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(changes))]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-sonnet-4-6"

        with (
            patch.object(agent, "_call_claude_with_thinking") as mock_thinking,
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=MagicMock(
                messages=MagicMock(create=MagicMock(return_value=mock_response))
            )),
        ):
            await agent._generate_changes_blocking(task, {}, {}, MagicMock(), complexity=2)

        mock_thinking.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_thinking_trace_when_thinking_is_empty(self):
        agent = self._make_builder()
        task = self._make_task(complexity=4)
        changes = {"summary": "x", "commit_message": "feat: y", "files": []}

        trace_calls: list = []

        async def _capture_trace(trace_type, content, context=None):
            trace_calls.append(trace_type)

        with (
            patch.object(agent, "_call_claude_with_thinking", return_value=(json.dumps(changes), "")),
            patch.object(agent, "_trace", side_effect=_capture_trace),
        ):
            await agent._generate_changes_blocking(task, {}, {}, MagicMock(), complexity=4)

        # No "decision" trace when thinking text is empty
        assert "decision" not in trace_calls
