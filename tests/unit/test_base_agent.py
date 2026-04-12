"""
Unit tests for phalanx/agents/base.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.base import (
    AgentResult,
    BaseAgent,
    get_anthropic_client,
    mark_run_failed,
    mark_task_failed,
)

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


# ── _call_claude (API fallback path) ──────────────────────────────────────────


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

        # Disable CLI so this test exercises the API fallback path
        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
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

        # Disable CLI so this test exercises the API fallback path
        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
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


# ── Claude Code CLI path ───────────────────────────────────────────────────────


class TestCallClaudeCLI:
    """Tests for the Claude Code CLI primary path and API fallback logic."""

    def _make_cli_response(
        self, result_text: str, input_tokens: int = 10, output_tokens: int = 5
    ) -> str:
        import json

        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": result_text,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "modelUsage": {
                    "claude-opus-4-6": {"inputTokens": input_tokens, "outputTokens": output_tokens}
                },
                "total_cost_usd": 0.01,
            }
        )

    def test_cli_used_when_available(self):
        """CLI path is taken when binary exists."""
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)
        cli_output = self._make_cli_response("Hello from CLI")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = cli_output

        with (
            patch("phalanx.agents.base._claude_cli_path", "/fake/claude"),
            patch("phalanx.agents.base.subprocess.run", return_value=mock_proc),
        ):
            result = agent._call_claude(messages=[{"role": "user", "content": "Hi"}])

        assert result == "Hello from CLI"

    def test_cli_tracks_tokens(self):
        """Token counts from CLI response are accumulated."""
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)
        cli_output = self._make_cli_response("ok", input_tokens=80, output_tokens=40)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = cli_output

        with (
            patch("phalanx.agents.base._claude_cli_path", "/fake/claude"),
            patch("phalanx.agents.base.subprocess.run", return_value=mock_proc),
        ):
            agent._call_claude(messages=[{"role": "user", "content": "Test"}])

        assert agent._tokens_used == 120  # 80 + 40

    def test_cli_fallback_to_api_when_binary_missing(self):
        """Falls back to API when CLI binary is None."""
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="API response")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-opus-4-6"
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            result = agent._call_claude(messages=[{"role": "user", "content": "Hi"}])

        assert result == "API response"
        mock_client.messages.create.assert_called_once()

    def test_cli_fallback_to_api_on_nonzero_exit(self):
        """Falls back to API when CLI exits with non-zero code."""
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "auth error"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="API fallback")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-opus-4-6"
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with (
            patch("phalanx.agents.base._claude_cli_path", "/fake/claude"),
            patch("phalanx.agents.base.subprocess.run", return_value=mock_proc),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            result = agent._call_claude(messages=[{"role": "user", "content": "Hi"}])

        assert result == "API fallback"

    def test_cli_fallback_to_api_on_timeout(self):
        """Falls back to API when CLI subprocess times out."""
        import subprocess as sp

        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="API fallback on timeout")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-opus-4-6"
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with (
            patch("phalanx.agents.base._claude_cli_path", "/fake/claude"),
            patch(
                "phalanx.agents.base.subprocess.run",
                side_effect=sp.TimeoutExpired(cmd="claude", timeout=300),
            ),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            result = agent._call_claude(messages=[{"role": "user", "content": "Hi"}])

        assert result == "API fallback on timeout"

    def test_cli_fallback_to_api_on_is_error_response(self):
        """Falls back to API when CLI returns is_error=True."""
        import json

        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        error_output = json.dumps(
            {"type": "result", "subtype": "error", "is_error": True, "result": "oops"}
        )
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = error_output

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="API fallback on CLI error")]
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-opus-4-6"
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with (
            patch("phalanx.agents.base._claude_cli_path", "/fake/claude"),
            patch("phalanx.agents.base.subprocess.run", return_value=mock_proc),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
        ):
            result = agent._call_claude(messages=[{"role": "user", "content": "Hi"}])

        assert result == "API fallback on CLI error"

    def test_cli_multi_turn_messages_flattened(self):
        """Multi-turn messages are flattened into a single prompt for CLI."""
        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)
        cli_output = self._make_cli_response("done")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = cli_output

        with (
            patch("phalanx.agents.base._claude_cli_path", "/fake/claude"),
            patch("phalanx.agents.base.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            agent._call_claude(
                messages=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply"},
                    {"role": "user", "content": "follow up"},
                ]
            )

        prompt_sent = mock_run.call_args.kwargs["input"]
        assert "USER: first" in prompt_sent
        assert "ASSISTANT: reply" in prompt_sent
        assert "USER: follow up" in prompt_sent

    def test_find_claude_cli_returns_none_when_missing(self):
        """_find_claude_cli returns None when binary is nowhere."""
        import phalanx.agents.base as base_module

        with (
            patch("phalanx.agents.base.shutil.which", return_value=None),
            patch("phalanx.agents.base.os.path.isfile", return_value=False),
            patch("phalanx.agents.base.glob.glob", return_value=[]),
        ):
            result = base_module._find_claude_cli()
        assert result is None


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

        with patch("phalanx.db.session.get_db", mock_get_db):
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

        with patch("phalanx.db.session.get_db", failing_get_db):
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
            patch("phalanx.db.session.get_db", mock_get_db),
            patch.object(agent, "_audit", AsyncMock()),
        ):
            # INTAKE → RESEARCHING is a valid transition
            await agent._transition_run("INTAKE", "RESEARCHING")

        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    async def test_invalid_transition_raises(self):
        from phalanx.workflow.state_machine import InvalidTransitionError

        agent = ConcreteAgent(run_id="r1", agent_id="tester")

        # RESEARCHING → INTAKE is an invalid non-terminal transition
        with pytest.raises(InvalidTransitionError):
            await agent._transition_run("RESEARCHING", "INTAKE")


# ── get_anthropic_client singleton ────────────────────────────────────────────


class TestGetAnthropicClient:
    def test_returns_same_instance(self):
        import phalanx.agents.base as base_module

        # Reset singleton
        base_module._anthropic_client = None
        with patch("phalanx.agents.base.Anthropic") as mock_anthropic:  # noqa: N806
            mock_anthropic.return_value = MagicMock()
            c1 = get_anthropic_client()
            c2 = get_anthropic_client()
        assert c1 is c2


# ── Retry policy ───────────────────────────────────────────────────────────────


class TestRetryPolicy:
    """_ANTHROPIC_RETRY now covers InternalServerError and APIConnectionError."""

    def test_retries_on_internal_server_error(self):
        """InternalServerError (HTTP 500) is retried up to max_retries times."""
        from anthropic import InternalServerError

        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        call_count = 0
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="ok")]
        mock_response.usage.input_tokens = 5
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-opus-4-6"

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # InternalServerError needs a response object with status_code
                mock_http_resp = MagicMock()
                mock_http_resp.status_code = 500
                raise InternalServerError("Internal server error", response=mock_http_resp, body={})
            return mock_response

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _side_effect

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            # Skip tenacity sleep so the test is instant
            patch("tenacity.nap.time.sleep"),
        ):
            result = agent._call_claude_api(messages=[{"role": "user", "content": "hi"}])

        assert result == "ok"
        assert call_count == 2  # failed once, succeeded on retry

    def test_retries_on_api_connection_error(self):
        """APIConnectionError (network drop) is retried."""
        from anthropic import APIConnectionError

        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)

        call_count = 0
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="ok")]
        mock_response.usage.input_tokens = 5
        mock_response.usage.output_tokens = 5
        mock_response.model = "claude-opus-4-6"

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise APIConnectionError(request=MagicMock())
            return mock_response

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _side_effect

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("tenacity.nap.time.sleep"),
        ):
            result = agent._call_claude_api(messages=[{"role": "user", "content": "hi"}])

        assert result == "ok"
        assert call_count == 2

    def test_does_not_retry_on_auth_error(self):
        """AuthenticationError is NOT in the retry list — should propagate immediately."""
        from anthropic import AuthenticationError

        agent = ConcreteAgent(run_id="r1", agent_id="tester", token_budget=100_000)
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_http_resp = MagicMock()
            mock_http_resp.status_code = 401
            raise AuthenticationError("Invalid API key", response=mock_http_resp, body={})

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _side_effect

        with (
            patch("phalanx.agents.base._claude_cli_path", None),
            patch("phalanx.agents.base.get_anthropic_client", return_value=mock_client),
            patch("tenacity.nap.time.sleep"),
            pytest.raises(AuthenticationError),
        ):
            agent._call_claude_api(messages=[{"role": "user", "content": "hi"}])

        assert call_count == 1  # no retry


# ── mark_task_failed / mark_run_failed ────────────────────────────────────────


class TestMarkTaskFailed:
    async def test_marks_task_failed_in_db(self):
        """mark_task_failed updates Task.status to FAILED with error message."""
        from contextlib import asynccontextmanager

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("phalanx.db.session.get_db", mock_get_db):
            await mark_task_failed("task-123", "Anthropic 500 error")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    async def test_non_fatal_when_db_unavailable(self):
        """mark_task_failed does NOT raise even if the DB write fails."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def failing_get_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_get_db):
            # Must not raise
            await mark_task_failed("task-xyz", "some error")

    async def test_error_truncated_to_2000_chars(self):
        """Very long error strings are truncated so they don't blow up the DB column."""
        from contextlib import asynccontextmanager

        captured_values: dict = {}
        mock_session = AsyncMock()

        async def _capture_execute(stmt):
            # Pull out the values dict from the compiled UPDATE statement
            captured_values["called"] = True
            return MagicMock()

        mock_session.execute = _capture_execute
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        long_error = "x" * 10_000
        with patch("phalanx.db.session.get_db", mock_get_db):
            await mark_task_failed("task-abc", long_error)

        assert captured_values.get("called")


class TestMarkRunFailed:
    async def test_marks_run_failed_in_db(self):
        """mark_run_failed updates Run.status to FAILED."""
        from contextlib import asynccontextmanager

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("phalanx.db.session.get_db", mock_get_db):
            await mark_run_failed("run-456", "Unhandled exception in commander")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    async def test_non_fatal_when_db_unavailable(self):
        """mark_run_failed does NOT raise even if the DB write fails."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def failing_get_db():
            raise RuntimeError("DB down")
            yield  # type: ignore[misc]

        with patch("phalanx.db.session.get_db", failing_get_db):
            await mark_run_failed("run-xyz", "some error")  # must not raise

    async def test_skips_already_terminal_runs(self):
        """mark_run_failed uses a WHERE filter to skip already-FAILED/COMPLETED runs."""
        from contextlib import asynccontextmanager

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0  # no rows updated (already terminal)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with patch("phalanx.db.session.get_db", mock_get_db):
            await mark_run_failed("run-already-failed", "duplicate error")
