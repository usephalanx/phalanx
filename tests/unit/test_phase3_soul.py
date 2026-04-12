"""
Phase 3 soul tests:
  #6 — Planner reflection (_reflect called before _generate_plan)
  #7 — Builder handoff notes + Reviewer loads them
  #1 — Self-check fix loop (_self_check_has_issues + _fix_self_check_issues)
"""

from __future__ import annotations

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


def make_agent(**kwargs):
    return ConcreteAgent(run_id="run-1", agent_id="builder", task_id="task-1", **kwargs)


def make_builder(**kwargs):
    return BuilderAgent(run_id="run-1", agent_id="builder", task_id="task-1", **kwargs)


def make_reviewer(**kwargs):
    return ReviewerAgent(run_id="run-1", agent_id="reviewer", task_id="task-1", **kwargs)


def make_task_orm(
    task_id="t-1",
    agent_role="builder",
    sequence_num=2,
    status="COMPLETED",
    output=None,
    title="Test task",
    description="Do stuff",
    files_likely_touched=None,
    estimated_complexity=3,
):
    t = MagicMock()
    t.id = task_id
    t.agent_role = agent_role
    t.sequence_num = sequence_num
    t.status = status
    t.output = output or {}
    t.title = title
    t.description = description
    t.files_likely_touched = files_likely_touched or []
    t.estimated_complexity = estimated_complexity
    t.role_context = None
    return t


def make_trace_orm(
    trace_id="tr-1",
    run_id="run-1",
    task_id="task-2",
    agent_role="builder",
    trace_type="handoff",
    content="I built X. Uncertain about Y. Focus on Z.",
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


# ── #6 Planner reflection tests ───────────────────────────────────────────────


class TestPlannerReflection:
    def test_planner_has_planner_soul_import(self):
        """PlannerAgent imports PLANNER_SOUL."""
        from phalanx.agents.planner import PlannerAgent  # noqa: F401
        import inspect
        import phalanx.agents.planner as planner_mod
        src = inspect.getsource(planner_mod)
        assert "PLANNER_SOUL" in src

    def test_planner_calls_reflect_before_generate_plan(self):
        """PlannerAgent.execute() calls _reflect() before _generate_plan()."""
        import inspect
        import phalanx.agents.planner as planner_mod
        src = inspect.getsource(planner_mod.PlannerAgent.execute)
        reflect_pos = src.find("_reflect(")
        generate_pos = src.find("_generate_plan(")
        assert reflect_pos != -1, "_reflect not found in execute"
        assert generate_pos != -1, "_generate_plan not found in execute"
        assert reflect_pos < generate_pos, "_reflect should come before _generate_plan"

    def test_planner_traces_reflection_when_result(self):
        """execute() calls _trace('reflection', ...) when reflection non-empty."""
        import inspect
        import phalanx.agents.planner as planner_mod
        src = inspect.getsource(planner_mod.PlannerAgent.execute)
        assert "_trace" in src
        assert '"reflection"' in src

    @pytest.mark.asyncio
    async def test_planner_reflection_uses_planner_soul(self):
        """_reflect is called with PLANNER_SOUL as soul arg."""
        from phalanx.agents.planner import PlannerAgent

        agent = PlannerAgent(run_id="run-1", agent_id="planner", task_id="task-1")

        task = make_task_orm(agent_role="planner")
        run = MagicMock()
        run.project_id = "proj-1"
        run.active_branch = None

        mock_plan = {
            "task_title": "T",
            "approach": "A",
            "files": [],
            "implementation_steps": ["S1"],
            "test_strategy": "TS",
            "acceptance_criteria": ["AC1"],
            "edge_cases": [],
            "estimated_complexity": 2,
        }

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.execute = AsyncMock(return_value=MagicMock(
                scalar_one_or_none=lambda: task,
                scalar_one=lambda: run,
                scalars=lambda: MagicMock(all=lambda: []),
            ))
            s.commit = AsyncMock()
            yield s

        with patch("phalanx.agents.planner.get_db", fake_db), \
             patch.object(agent, "_reflect", return_value="reflection text") as mock_reflect, \
             patch.object(agent, "_trace", new_callable=AsyncMock) as mock_trace, \
             patch.object(agent, "_generate_plan", new_callable=AsyncMock, return_value=mock_plan), \
             patch.object(agent, "_persist_artifact", new_callable=AsyncMock), \
             patch.object(agent, "_audit", new_callable=AsyncMock):
            await agent.execute()

        mock_reflect.assert_called_once()
        call_kwargs = mock_reflect.call_args[1] if mock_reflect.call_args[1] else {}
        call_args = mock_reflect.call_args[0] if mock_reflect.call_args[0] else ()
        # soul param should be PLANNER_SOUL
        from phalanx.agents.soul import PLANNER_SOUL
        soul_passed = call_kwargs.get("soul") or (call_args[2] if len(call_args) > 2 else None)
        assert soul_passed == PLANNER_SOUL

        # Trace should have been called with "reflection"
        mock_trace.assert_called_once()
        assert mock_trace.call_args[0][0] == "reflection"


# ── #7 Builder handoff notes tests ────────────────────────────────────────────


class TestBuilderHandoffNotes:
    def test_write_handoff_note_returns_empty_when_no_files(self):
        agent = make_builder()
        with patch.object(agent, "_call_claude", return_value="some text"):
            result = agent._write_handoff_note(
                task_description="Test",
                files_written=[],
                summary="",
                self_check_result="",
            )
        assert result == ""

    def test_write_handoff_note_calls_claude(self):
        agent = make_builder()
        with patch.object(agent, "_call_claude", return_value="Handoff note text.") as mock_call:
            result = agent._write_handoff_note(
                task_description="Build a router",
                files_written=["app.py"],
                summary="Added router",
                self_check_result="",
            )
        assert result == "Handoff note text."
        mock_call.assert_called_once()

    def test_write_handoff_note_includes_self_check_issues(self):
        """If self-check found issues, they appear in the handoff prompt."""
        agent = make_builder()
        captured_prompt = {}

        def capture_call(**kwargs):
            captured_prompt["content"] = kwargs["messages"][0]["content"]
            return "Handoff with issues noted."

        with patch.object(agent, "_call_claude", side_effect=capture_call):
            agent._write_handoff_note(
                task_description="Build a router",
                files_written=["app.py"],
                summary="Added router",
                self_check_result="Import error: foo.py not found",
            )

        assert "Import error" in captured_prompt["content"]

    def test_write_handoff_note_on_claude_failure_returns_empty(self):
        agent = make_builder()
        with patch.object(agent, "_call_claude", side_effect=Exception("API error")):
            result = agent._write_handoff_note(
                task_description="Test",
                files_written=["file.py"],
                summary="",
                self_check_result="",
            )
        assert result == ""

    def test_write_handoff_note_includes_passed_check(self):
        """Self-check passed → 'Self-check: passed.' in handoff prompt."""
        agent = make_builder()
        captured_prompt = {}

        def capture_call(**kwargs):
            captured_prompt["content"] = kwargs["messages"][0]["content"]
            return "Handoff note."

        with patch.object(agent, "_call_claude", side_effect=capture_call):
            agent._write_handoff_note(
                task_description="T",
                files_written=["f.py"],
                summary="done",
                self_check_result="Self-check passed.",
            )

        assert "Self-check: passed." in captured_prompt["content"]

    @pytest.mark.asyncio
    async def test_reviewer_load_builder_handoff_returns_content(self):
        """_load_builder_handoff returns content of latest handoff trace."""
        reviewer = make_reviewer()
        trace = make_trace_orm(content="Builder handoff: built auth module.")

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.execute = AsyncMock(
                return_value=MagicMock(scalar_one_or_none=lambda: trace)
            )
            yield s

        with patch("phalanx.agents.reviewer.get_db", fake_db):
            result = await reviewer._load_builder_handoff(before_seq=5)

        assert result == "Builder handoff: built auth module."

    @pytest.mark.asyncio
    async def test_reviewer_load_builder_handoff_returns_empty_when_none(self):
        """_load_builder_handoff returns '' when no handoff trace exists."""
        reviewer = make_reviewer()

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.execute = AsyncMock(
                return_value=MagicMock(scalar_one_or_none=lambda: None)
            )
            yield s

        with patch("phalanx.agents.reviewer.get_db", fake_db):
            result = await reviewer._load_builder_handoff(before_seq=5)

        assert result == ""

    @pytest.mark.asyncio
    async def test_reviewer_load_builder_handoff_returns_empty_on_error(self):
        """_load_builder_handoff returns '' on DB error (non-fatal)."""
        reviewer = make_reviewer()

        @asynccontextmanager
        async def fake_db():
            s = AsyncMock()
            s.execute = AsyncMock(side_effect=Exception("DB exploded"))
            yield s

        with patch("phalanx.agents.reviewer.get_db", fake_db):
            result = await reviewer._load_builder_handoff(before_seq=5)

        assert result == ""

    def test_run_review_includes_handoff_section(self):
        """_run_review injects builder_handoff into the user message."""
        import inspect
        import phalanx.agents.reviewer as rev_mod
        src = inspect.getsource(rev_mod.ReviewerAgent._run_review)
        assert "builder_handoff" in src
        assert "handoff_section" in src

    def test_run_review_signature_accepts_handoff(self):
        """_run_review accepts builder_handoff param (default '')."""
        import inspect
        from phalanx.agents.reviewer import ReviewerAgent
        sig = inspect.signature(ReviewerAgent._run_review)
        assert "builder_handoff" in sig.parameters


# ── #1 Self-check fix loop tests ─────────────────────────────────────────────


class TestSelfCheckFixLoop:
    def test_self_check_has_issues_returns_false_for_empty(self):
        agent = make_builder()
        assert agent._self_check_has_issues("") is False

    def test_self_check_has_issues_returns_false_for_pass(self):
        agent = make_builder()
        assert agent._self_check_has_issues("Self-check passed.") is False

    def test_self_check_has_issues_returns_false_for_pass_case_insensitive(self):
        agent = make_builder()
        assert agent._self_check_has_issues("SELF-CHECK PASSED. All looks good.") is False

    def test_self_check_has_issues_returns_true_for_issues(self):
        agent = make_builder()
        assert agent._self_check_has_issues("Import error in app.py: foo not found") is True

    def test_self_check_has_issues_returns_true_for_partial_pass_plus_issues(self):
        """If there's text beyond 'self-check passed', still detect issues."""
        agent = make_builder()
        # A message that contains issues alongside or before pass phrase is still an issue
        result = "Import error found. Self-check failed."
        assert agent._self_check_has_issues(result) is True

    @pytest.mark.asyncio
    async def test_fix_self_check_issues_calls_claude(self):
        """_fix_self_check_issues calls _call_claude and returns parsed changes."""
        agent = make_builder()
        task = make_task_orm()
        plan = {"approach": "simple"}
        existing_files = {"app.py": "# content"}

        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            fix_result = {
                "summary": "Fixed import",
                "commit_message": "fix: import",
                "files": [{"path": "app.py", "action": "modify", "content": "# fixed"}],
            }
            import json as _json
            with patch.object(agent, "_call_claude", return_value=_json.dumps(fix_result)):
                result = await agent._fix_self_check_issues(
                    task, plan, existing_files, workspace,
                    self_check_result="Import error in app.py",
                )
            assert result["summary"] == "Fixed import"
            assert len(result["files"]) == 1

    @pytest.mark.asyncio
    async def test_fix_self_check_issues_returns_empty_on_failure(self):
        """_fix_self_check_issues returns {} on API error (non-fatal)."""
        agent = make_builder()
        task = make_task_orm()
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with patch.object(agent, "_call_claude", side_effect=Exception("API down")):
                result = await agent._fix_self_check_issues(
                    task, {}, {}, workspace,
                    self_check_result="Import error",
                )
            assert result == {}

    @pytest.mark.asyncio
    async def test_fix_self_check_issues_injects_issues_in_prompt(self):
        """The self-check issues text is included in the fix prompt."""
        agent = make_builder()
        task = make_task_orm()
        captured = {}

        import json as _json, tempfile
        from pathlib import Path

        def capture_call(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _json.dumps({"summary": "fixed", "commit_message": "fix", "files": []})

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(agent, "_call_claude", side_effect=capture_call):
                await agent._fix_self_check_issues(
                    task, {}, {}, Path(tmpdir),
                    self_check_result="Missing import for utils.py",
                )

        user_content = captured["messages"][-1]["content"]
        assert "Missing import for utils.py" in user_content

    def test_execute_calls_self_check_has_issues(self):
        """execute() references _self_check_has_issues — verify it's in source."""
        import inspect
        import phalanx.agents.builder as builder_mod
        src = inspect.getsource(builder_mod.BuilderAgent.execute)
        assert "_self_check_has_issues" in src

    def test_execute_calls_fix_self_check_issues(self):
        """execute() calls _fix_self_check_issues when issues found."""
        import inspect
        import phalanx.agents.builder as builder_mod
        src = inspect.getsource(builder_mod.BuilderAgent.execute)
        assert "_fix_self_check_issues" in src

    def test_execute_calls_write_handoff_note(self):
        """execute() calls _write_handoff_note after self-check."""
        import inspect
        import phalanx.agents.builder as builder_mod
        src = inspect.getsource(builder_mod.BuilderAgent.execute)
        assert "_write_handoff_note" in src

    def test_fix_loop_limited_to_one_attempt(self):
        """_fix_self_check_issues is called at most once in execute() (no loop)."""
        import inspect
        import phalanx.agents.builder as builder_mod
        src = inspect.getsource(builder_mod.BuilderAgent.execute)
        # Exactly one call to _fix_self_check_issues (not in a while/for loop)
        assert src.count("_fix_self_check_issues") == 1
