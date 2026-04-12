"""
Unit tests for UX Designer agent and commander injection.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.commander import _inject_ux_designer_task
from phalanx.agents.ux_designer import UXDesignerAgent, is_ui_project

# ── is_ui_project ─────────────────────────────────────────────────────────────


class TestIsUiProject:
    def test_web_app_detected(self):
        assert is_ui_project("Build a todo web app") is True

    def test_mobile_app_detected(self):
        assert is_ui_project("Build an iOS task manager") is True

    def test_react_detected(self):
        assert is_ui_project("Build a React dashboard") is True

    def test_pure_api_not_detected(self):
        assert is_ui_project("Build a REST API for payments") is False

    def test_description_used(self):
        assert is_ui_project("Build a service", "with a modern dashboard interface") is True

    def test_empty_is_not_ui(self):
        assert is_ui_project("") is False

    def test_flutter_detected(self):
        assert is_ui_project("Build a Flutter expense tracker") is True

    def test_landing_page_detected(self):
        assert is_ui_project("Create a marketing landing page") is True


# ── _inject_ux_designer_task ──────────────────────────────────────────────────


class TestInjectUxDesignerTask:
    def _base_plan(self):
        return {
            "tasks": [
                {"sequence_num": 1, "title": "Architecture plan", "agent_role": "planner", "depends_on": []},
                {"sequence_num": 2, "title": "Build components", "agent_role": "builder", "depends_on": [1]},
                {"sequence_num": 3, "title": "Code review", "agent_role": "reviewer", "depends_on": [2]},
            ]
        }

    def test_injects_ux_task_before_first_builder(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a todo webapp", "")
        roles = [t["agent_role"] for t in plan["tasks"]]
        ux_idx = roles.index("ux_designer")
        builder_idx = roles.index("builder")
        assert ux_idx < builder_idx

    def test_ux_task_gets_correct_sequence_num(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a todo webapp", "")
        ux_task = next(t for t in plan["tasks"] if t["agent_role"] == "ux_designer")
        assert ux_task["sequence_num"] == 2

    def test_builder_shifted_up_by_one(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a todo webapp", "")
        builder_task = next(t for t in plan["tasks"] if t["agent_role"] == "builder")
        assert builder_task["sequence_num"] == 3

    def test_reviewer_shifted_up_by_one(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a todo webapp", "")
        reviewer_task = next(t for t in plan["tasks"] if t["agent_role"] == "reviewer")
        assert reviewer_task["sequence_num"] == 4

    def test_depends_on_updated_correctly(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a todo webapp", "")
        builder_task = next(t for t in plan["tasks"] if t["agent_role"] == "builder")
        # builder was depends_on=[1], should now depend on ux_designer (seq 2)
        # original depends_on=[1] stays [1] because 1 < first_builder_seq(2)
        assert builder_task["depends_on"] == [1]

    def test_non_ui_project_unchanged(self):
        plan = self._base_plan()
        result = _inject_ux_designer_task(plan, "Build a REST API for payments", "")
        roles = [t["agent_role"] for t in result["tasks"]]
        assert "ux_designer" not in roles

    def test_empty_plan_unchanged(self):
        result = _inject_ux_designer_task({"tasks": []}, "Build a webapp", "")
        assert result == {"tasks": []}

    def test_no_builder_tasks_unchanged(self):
        plan = {"tasks": [
            {"sequence_num": 1, "title": "Plan", "agent_role": "planner", "depends_on": []}
        ]}
        result = _inject_ux_designer_task(plan, "Build a webapp", "")
        roles = [t["agent_role"] for t in result["tasks"]]
        assert "ux_designer" not in roles

    def test_ux_task_has_design_md_in_files(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a todo webapp", "")
        ux_task = next(t for t in plan["tasks"] if t["agent_role"] == "ux_designer")
        assert "DESIGN.md" in ux_task["files_likely_touched"]

    def test_tasks_remain_sorted_by_sequence_num(self):
        plan = _inject_ux_designer_task(self._base_plan(), "Build a webapp", "")
        seqs = [t["sequence_num"] for t in plan["tasks"]]
        assert seqs == sorted(seqs)


# ── UXDesignerAgent ───────────────────────────────────────────────────────────


def _make_agent():
    return UXDesignerAgent(run_id="run-1", agent_id="ux-designer", task_id="task-1")


class TestInferAppType:
    def test_ios_detected(self):
        agent = _make_agent()
        assert "iOS" in agent._infer_app_type("Build an iOS app", "")

    def test_android_detected(self):
        agent = _make_agent()
        assert "Android" in agent._infer_app_type("Build an Android app", "")

    def test_flutter_detected(self):
        agent = _make_agent()
        assert "Flutter" in agent._infer_app_type("Flutter expense tracker", "")

    def test_dashboard_detected(self):
        agent = _make_agent()
        assert "dashboard" in agent._infer_app_type("Build an admin dashboard", "").lower()

    def test_ecommerce_detected(self):
        agent = _make_agent()
        assert "e-commerce" in agent._infer_app_type("Build an online shop", "").lower()

    def test_default_is_web_application(self):
        agent = _make_agent()
        assert agent._infer_app_type("Build a todo app", "") == "web application"


class TestInferAudience:
    def test_enterprise(self):
        agent = _make_agent()
        assert "business" in agent._infer_audience("Build a B2B SaaS dashboard").lower()

    def test_developer(self):
        agent = _make_agent()
        assert "developer" in agent._infer_audience("Build an API explorer for developers").lower()

    def test_default_consumer(self):
        agent = _make_agent()
        assert "consumer" in agent._infer_audience("Build a todo app").lower()


class TestFallbackDesign:
    def test_fallback_contains_design_sections(self):
        agent = _make_agent()
        content = agent._fallback_design("My App", "web application")
        assert "## 1. Brand Identity" in content
        assert "## 2. Color Palette" in content
        assert "## 5. Component Taxonomy" in content
        assert "## 6. Logo" in content
        assert "<svg" in content

    def test_fallback_uses_title(self):
        agent = _make_agent()
        content = agent._fallback_design("SuperApp", "web application")
        assert "SuperApp" in content


class TestSoulIntegration:
    """Verify the UX designer agent has soul: reflection, self-check, uncertainty escalation."""

    def test_agent_role_is_ux_designer(self):
        assert UXDesignerAgent.AGENT_ROLE == "ux_designer"

    def test_soul_prompts_registered(self):
        from phalanx.agents.soul import UX_DESIGNER_SOUL, get_reflection_prompt, get_soul
        assert get_soul("ux_designer") == UX_DESIGNER_SOUL
        assert get_reflection_prompt("ux_designer") is not None

    def test_ux_designer_soul_mentions_wcag(self):
        from phalanx.agents.soul import UX_DESIGNER_SOUL
        assert "WCAG" in UX_DESIGNER_SOUL

    def test_ux_designer_soul_is_language_agnostic(self):
        from phalanx.agents.soul import UX_DESIGNER_SOUL
        # Soul should mention no code
        assert "never write code" in UX_DESIGNER_SOUL.lower() or "never writes code" in UX_DESIGNER_SOUL.lower()

    def test_self_check_prompt_has_contrast_check(self):
        from phalanx.agents.soul import UX_DESIGNER_SELF_CHECK_PROMPT
        assert "WCAG" in UX_DESIGNER_SELF_CHECK_PROMPT or "contrast" in UX_DESIGNER_SELF_CHECK_PROMPT.lower()

    def test_reflection_prompt_asks_about_audience(self):
        from phalanx.agents.soul import UX_DESIGNER_REFLECTION_PROMPT
        assert "audience" in UX_DESIGNER_REFLECTION_PROMPT.lower() or "user" in UX_DESIGNER_REFLECTION_PROMPT.lower()

    @pytest.mark.asyncio
    async def test_execute_writes_design_md(self):
        """execute() writes DESIGN.md to workspace and marks task COMPLETED."""
        import tempfile

        agent = _make_agent()

        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.title = "Build a todo webapp"
        mock_task.description = "A simple todo app"
        mock_task.sequence_num = 2

        mock_run = MagicMock()
        mock_run.project_id = "proj-1"
        mock_run.id = "run-1"

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        ))

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("phalanx.agents.ux_designer.settings") as mock_settings,
            patch("phalanx.agents.ux_designer.get_db", mock_get_db),
            patch.object(agent, "_load_task", return_value=mock_task),
            patch.object(agent, "_load_run", return_value=mock_run),
            patch.object(agent, "_load_planner_context", return_value=""),
            patch.object(agent, "_reflect", return_value="This is a web todo app."),
            patch.object(agent, "_trace", new_callable=AsyncMock),
            patch.object(agent, "_generate_design", return_value="# DESIGN.md\n## 1. Brand Identity\nApp: Todo\n## 6. Logo\n<svg></svg>"),
            patch.object(agent, "_self_check_design", return_value="Design self-check passed."),
            patch.object(agent, "_write_design_handoff", return_value="Clean minimal design."),
            patch.object(agent, "_persist_design_artifact", new_callable=AsyncMock),
        ):
            mock_settings.git_workspace = tmpdir
            result = await agent.execute()

        assert result.success is True
        assert "files_written" in result.output
        assert "DESIGN.md" in result.output["files_written"]

    @pytest.mark.asyncio
    async def test_uncertainty_trace_emitted_when_reflection_flags_vagueness(self):
        """If reflection mentions 'underspecified', an uncertainty trace is emitted."""
        agent = _make_agent()
        traces_emitted = []

        async def capture_trace(trace_type, content, context=None):
            traces_emitted.append(trace_type)

        mock_task = MagicMock()
        mock_task.title = "Build something"
        mock_task.description = ""
        mock_task.sequence_num = 1

        mock_run = MagicMock()
        mock_run.project_id = "proj-1"
        mock_run.id = "run-1"

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
        ))

        @asynccontextmanager
        async def mock_get_db():
            yield mock_session

        import tempfile
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("phalanx.agents.ux_designer.settings") as mock_settings,
            patch("phalanx.agents.ux_designer.get_db", mock_get_db),
            patch.object(agent, "_load_task", return_value=mock_task),
            patch.object(agent, "_load_run", return_value=mock_run),
            patch.object(agent, "_load_planner_context", return_value=""),
            patch.object(agent, "_reflect", return_value="This brief is underspecified — I cannot determine the target audience."),
            patch.object(agent, "_trace", side_effect=capture_trace),
            patch.object(agent, "_generate_design", return_value="# DESIGN.md\n## 6. Logo\n<svg></svg>"),
            patch.object(agent, "_self_check_design", return_value="Design self-check passed."),
            patch.object(agent, "_write_design_handoff", return_value=""),
            patch.object(agent, "_persist_design_artifact", new_callable=AsyncMock),
        ):
            mock_settings.git_workspace = tmpdir
            await agent.execute()

        assert "uncertainty" in traces_emitted
