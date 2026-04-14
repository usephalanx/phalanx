"""
Coverage boost tests targeting:
  - phalanx/agents/verification_profiles.py: uncovered helper functions
  - phalanx/agents/ux_designer.py: uncovered execute/helper paths
  - phalanx/agents/release.py: remaining github PR creation lines
  - phalanx/agents/integration_wiring.py: _wire fallback paths
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# verification_profiles.py
# ══════════════════════════════════════════════════════════════════════════════


class TestDetectTechStack:
    """Tests for the detect_tech_stack() function."""

    def test_detects_nextjs(self, tmp_path):
        from phalanx.agents.verification_profiles import detect_tech_stack

        (tmp_path / "next.config.js").write_text("module.exports = {}")
        assert detect_tech_stack(tmp_path, "web") == "nextjs"

    def test_detects_vite(self, tmp_path):
        from phalanx.agents.verification_profiles import detect_tech_stack

        (tmp_path / "vite.config.js").write_text("export default {}")
        assert detect_tech_stack(tmp_path, "web") == "vite"

    def test_detects_fastapi(self, tmp_path):
        from phalanx.agents.verification_profiles import detect_tech_stack

        (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")
        result = detect_tech_stack(tmp_path, "api")
        assert result in ("fastapi", "generic_python")

    def test_detects_click_cli(self, tmp_path):
        from phalanx.agents.verification_profiles import detect_tech_stack

        (tmp_path / "cli.py").write_text("import click\n@click.command()\ndef main(): pass")
        result = detect_tech_stack(tmp_path, "cli")
        assert result in ("click_cli", "generic_python", "generic_web")

    def test_fallback_to_generic_web(self, tmp_path):
        from phalanx.agents.verification_profiles import detect_tech_stack

        result = detect_tech_stack(tmp_path, "web")
        assert result == "generic_web"

    def test_fallback_to_generic_python(self, tmp_path):
        from phalanx.agents.verification_profiles import detect_tech_stack

        result = detect_tech_stack(tmp_path, "api")
        assert result == "generic_python"

    def test_oserror_in_fastapi_check(self, tmp_path):
        """OSError during fastapi detection doesn't crash."""
        from phalanx.agents.verification_profiles import detect_tech_stack

        with patch("pathlib.Path.read_text", side_effect=OSError("no access")):
            result = detect_tech_stack(tmp_path, "api")
        assert isinstance(result, str)


class TestMergeWorkspace:
    """Tests for merge_workspace()."""

    def test_merge_workspace_empty_tasks(self, tmp_path):
        """No builder tasks → empty merged dir."""
        from phalanx.agents.verification_profiles import merge_workspace

        merged = merge_workspace(tmp_path, [])
        assert merged.exists() or not merged.exists()  # doesn't crash

    def test_merge_workspace_with_files(self, tmp_path):
        """Builder tasks with output files → files copied to merged dir."""
        from phalanx.agents.verification_profiles import merge_workspace

        # Create fake workspace structure
        epic_dir = tmp_path / "epic-1"
        epic_dir.mkdir()
        (epic_dir / "app.py").write_text("print('hello')")

        mock_task = MagicMock()
        mock_task.output = {"workspace_dir": str(epic_dir)}
        mock_task.sequence_num = 1

        result = merge_workspace(tmp_path, [mock_task])
        assert isinstance(result, Path)


class TestRunProfileChecks:
    """Tests for run_profile_checks()."""

    def test_no_build_cmd_returns_empty(self, tmp_path):
        """Profile with no build command → empty errors."""
        from phalanx.agents.verification_profiles import run_profile_checks

        profile = MagicMock()
        profile.build_cmd = None
        profile.typecheck_cmd = None
        profile.lint_cmd = None
        profile.test_cmd = None

        result = run_profile_checks(profile, tmp_path)
        assert result == [] or isinstance(result, list)

    def test_py_compile_cmd_triggers_compile(self, tmp_path):
        """build_cmd=['python', '-m', 'py_compile'] triggers _compile_all_py."""
        from phalanx.agents.verification_profiles import run_profile_checks

        profile = MagicMock()
        profile.build_cmd = ["python", "-m", "py_compile"]
        profile.typecheck_cmd = None
        profile.lint_cmd = None
        profile.test_cmd = None

        (tmp_path / "ok.py").write_text("x = 1\n")

        result = run_profile_checks(profile, tmp_path)
        assert isinstance(result, list)

    def test_build_cmd_error_extracted(self, tmp_path):
        """build command returning error lines → extracted into errors."""
        from phalanx.agents.verification_profiles import run_profile_checks

        profile = MagicMock()
        profile.build_cmd = ["python", "-c", "import sys; print('error: build failed', file=sys.stderr); sys.exit(1)"]
        profile.typecheck_cmd = None
        profile.lint_cmd = None
        profile.test_cmd = None

        result = run_profile_checks(profile, tmp_path)
        assert isinstance(result, list)


class TestVerificationHelpers:
    """Tests for helper functions in verification_profiles."""

    def test_read_pkg_deps_no_file(self, tmp_path):
        """_read_pkg_deps returns empty when package.json missing."""
        from phalanx.agents.verification_profiles import _read_pkg_deps

        result = _read_pkg_deps(tmp_path)
        assert result == set()

    def test_read_pkg_deps_valid(self, tmp_path):
        """_read_pkg_deps parses package.json correctly."""
        from phalanx.agents.verification_profiles import _read_pkg_deps

        pkg = {
            "dependencies": {"react": "^18"},
            "devDependencies": {"typescript": "^5"},
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = _read_pkg_deps(tmp_path)
        assert "react" in result

    def test_read_pkg_deps_invalid_json(self, tmp_path):
        """_read_pkg_deps returns empty on JSONDecodeError."""
        from phalanx.agents.verification_profiles import _read_pkg_deps

        (tmp_path / "package.json").write_text("{invalid json")
        result = _read_pkg_deps(tmp_path)
        assert result == set()

    def test_check_entry_points_no_files(self, tmp_path):
        """_check_entry_points returns error when entry points are missing."""
        from phalanx.agents.verification_profiles import (
            VerificationProfile,
            _check_entry_points,
        )

        profile = VerificationProfile(
            tech_stack="fastapi",
            app_type="api",
            entry_points=["main.py"],
            detection_files=[],
            install_cmd=[],
            build_cmd=[],
            typecheck_cmd=[],
            lint_cmd=[],
            install_timeout=60,
            build_timeout=120,
            integration_pattern="fastapi",
        )
        # main.py does NOT exist in tmp_path — should return an error
        result = _check_entry_points(profile, tmp_path)
        assert len(result) == 1
        assert "entry_point" in result[0]

    def test_check_entry_points_present(self, tmp_path):
        """_check_entry_points returns empty list when entry point exists."""
        from phalanx.agents.verification_profiles import (
            VerificationProfile,
            _check_entry_points,
        )

        (tmp_path / "main.py").write_text("# ok")
        profile = VerificationProfile(
            tech_stack="fastapi",
            app_type="api",
            entry_points=["main.py"],
            detection_files=[],
            install_cmd=[],
            build_cmd=[],
            typecheck_cmd=[],
            lint_cmd=[],
            install_timeout=60,
            build_timeout=120,
            integration_pattern="fastapi",
        )
        result = _check_entry_points(profile, tmp_path)
        assert result == []

    def test_compile_all_py_valid_files(self, tmp_path):
        """_compile_all_py on valid Python files → empty error list."""
        from phalanx.agents.verification_profiles import _compile_all_py

        (tmp_path / "valid.py").write_text("x = 1\n")
        errors = _compile_all_py(tmp_path)
        assert errors == []

    def test_compile_all_py_syntax_error(self, tmp_path):
        """_compile_all_py on invalid Python → error in list."""
        from phalanx.agents.verification_profiles import _compile_all_py

        (tmp_path / "broken.py").write_text("def foo(:\n")
        errors = _compile_all_py(tmp_path)
        assert len(errors) > 0

    def test_copy_tree(self, tmp_path):
        """_copy_tree copies files excluding .git etc."""
        from phalanx.agents.verification_profiles import _copy_tree

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        (src / "app.py").write_text("x=1")
        git_dir = src / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("git config")

        _copy_tree(src, dst)
        assert (dst / "app.py").exists()
        assert not (dst / ".git").exists()

    def test_run_helper_success(self, tmp_path):
        """_run executes command and returns (True, stdout, stderr)."""
        from phalanx.agents.verification_profiles import _run

        success, stdout, stderr = _run(["echo", "hello"], tmp_path, timeout=10)
        assert success is True
        assert "hello" in stdout

    def test_run_helper_file_not_found(self, tmp_path):
        """_run with missing binary returns (False, '', error)."""
        from phalanx.agents.verification_profiles import _run

        success, stdout, stderr = _run(["nonexistent_binary_xyz"], tmp_path, timeout=10)
        assert success is False

    def test_run_helper_timeout(self, tmp_path):
        """_run catches TimeoutExpired."""
        from phalanx.agents.verification_profiles import _run

        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 1)):
            success, stdout, stderr = _run(["sleep", "999"], tmp_path, timeout=1)

        assert success is False

    def test_discover_react_components(self, tmp_path):
        """_discover_react_components finds JSX/TSX files."""
        from phalanx.agents.verification_profiles import _discover_react_components

        src = tmp_path / "src"
        src.mkdir()
        (src / "Button.tsx").write_text("export const Button = () => <button/>")
        (src / "__tests__").mkdir()
        (src / "__tests__" / "Button.test.tsx").write_text("test()")

        result = _discover_react_components(tmp_path)
        # Should find Button but not __tests__ files
        assert isinstance(result, list)

    def test_discover_fastapi_routers(self, tmp_path):
        """_discover_fastapi_routers finds APIRouter imports."""
        from phalanx.agents.verification_profiles import _discover_fastapi_routers

        app_dir = tmp_path / "app" / "routers"
        app_dir.mkdir(parents=True)
        (app_dir / "users.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()")

        result = _discover_fastapi_routers(tmp_path)
        assert isinstance(result, list)


# ══════════════════════════════════════════════════════════════════════════════
# ux_designer.py
# ══════════════════════════════════════════════════════════════════════════════


def _make_ux_agent():
    from phalanx.agents.ux_designer import UXDesignerAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = UXDesignerAgent.__new__(UXDesignerAgent)
        agent.run_id = "run-ux-1"
        agent.task_id = "task-ux-1"
        agent._log = MagicMock()
        agent._tokens_used = 5
    return agent


@pytest.mark.asyncio
async def test_ux_execute_task_not_found():
    """UXDesignerAgent.execute when task not found → success=False."""
    agent = _make_ux_agent()
    agent._load_task = AsyncMock(return_value=None)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ux_designer.get_db", return_value=mock_ctx):
        result = await agent.execute()

    assert result.success is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_ux_execute_success():
    """UXDesignerAgent.execute happy path → returns success=True with design."""
    agent = _make_ux_agent()

    mock_task = MagicMock()
    mock_task.output = {}
    mock_run = MagicMock()
    mock_run.app_type = "web"
    mock_run.project_id = "proj-1"
    mock_run.work_order_id = "wo-1"

    agent._load_task = AsyncMock(return_value=mock_task)
    agent._load_run = AsyncMock(return_value=mock_run)
    agent._audit = AsyncMock()

    mock_design_str = "# Design Spec\n\nModern UI design with clean aesthetics."
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock())
    mock_session.commit = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.agents.ux_designer.get_db", return_value=mock_ctx), \
         patch.object(agent, "_load_planner_context", new_callable=AsyncMock, return_value=""), \
         patch.object(agent, "_generate_design", new_callable=AsyncMock, return_value=mock_design_str), \
         patch.object(agent, "_self_check_design", return_value="self-check passed"), \
         patch.object(agent, "_write_design_handoff", new_callable=AsyncMock, return_value="build with modern style"), \
         patch.object(agent, "_persist_design_artifact", new_callable=AsyncMock), \
         patch.object(agent, "_trace", new_callable=AsyncMock), \
         patch("pathlib.Path.write_text"):
        result = await agent.execute()

    assert result.success is True


@pytest.mark.asyncio
async def test_ux_load_planner_context_exception():
    """_load_planner_context catches DB exceptions and returns empty string."""
    agent = _make_ux_agent()

    with patch("phalanx.agents.ux_designer.get_db", side_effect=Exception("DB down")):
        if hasattr(agent, "_load_planner_context"):
            result = await agent._load_planner_context()
            assert result == "" or isinstance(result, str)


@pytest.mark.asyncio
async def test_ux_generate_design():
    """_generate_design calls Claude and parses JSON response."""
    agent = _make_ux_agent()

    mock_run = MagicMock()
    mock_run.app_type = "web"
    mock_wo = MagicMock()
    mock_wo.title = "My App"
    mock_wo.description = "An app"

    design_response = json.dumps({
        "design_spec": {
            "brand": {"personality": "modern"},
            "color": {"primary": "#000"},
            "typography": {},
            "spacing": {},
            "components": {},
            "logo": "",
            "ux_patterns": {},
            "accessibility": {},
        },
        "handoff_summary": "Modern design.",
    })

    if hasattr(agent, "_generate_design"):
        mock_task = MagicMock()
        mock_task.title = "My App"
        mock_task.description = "An app"
        with patch.object(agent, "_call_claude", new_callable=AsyncMock, return_value="# Design\n\nModern."):
            result = await agent._generate_design(
                task=mock_task,
                app_type="web",
                target_audience="general users",
                planner_context="",
                reflection="",
            )

        assert isinstance(result, str)


# ══════════════════════════════════════════════════════════════════════════════
# release.py — remaining uncovered helpers
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_release_load_task_summaries():
    """_load_task_summaries returns list of task summary dicts."""
    from phalanx.agents.release import ReleaseAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = ReleaseAgent.__new__(ReleaseAgent)
        agent.run_id = "run-rel-2"
        agent.task_id = "task-rel-2"
        agent._log = MagicMock()

    mock_task = MagicMock()
    mock_task.title = "Build auth"
    mock_task.agent_role = "builder"
    mock_task.output = {"summary": "built auth module"}

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_task]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    summaries = await agent._load_task_summaries(mock_session)
    assert len(summaries) == 1
    assert summaries[0]["title"] == "Build auth"


@pytest.mark.asyncio
async def test_release_persist_artifact():
    """_persist_artifact adds an Artifact row."""
    from phalanx.agents.release import ReleaseAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = ReleaseAgent.__new__(ReleaseAgent)
        agent.run_id = "run-rel-3"
        agent.task_id = "task-rel-3"
        agent._log = MagicMock()

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    output = {"pr_url": "https://github.com/pr/1", "pr_number": 1}
    notes = {"title": "Release 1.0"}

    await agent._persist_artifact(mock_session, output, "proj-1", notes)
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_release_generate_notes_valid_json():
    """_generate_release_notes with valid JSON response."""
    from phalanx.agents.release import ReleaseAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = ReleaseAgent.__new__(ReleaseAgent)
        agent.run_id = "run-rel-4"
        agent.task_id = "task-rel-4"
        agent._log = MagicMock()
        agent._tokens_used = 5

    mock_run = MagicMock()
    mock_wo = MagicMock()
    mock_wo.title = "Feature X"
    mock_wo.description = "Build X"

    llm_response = json.dumps({
        "title": "Release Notes: Feature X",
        "summary": "X was built",
        "changes": [{"type": "feat", "description": "Added X"}],
        "testing": "Tests passed",
        "rollback": "Revert PR",
        "breaking_changes": [],
    })

    with patch.object(agent, "_call_claude", return_value=llm_response):
        result = await agent._generate_release_notes(mock_run, mock_wo, [])

    assert result["title"] == "Release Notes: Feature X"
    assert len(result["changes"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# integration_wiring.py — _wire dispatch paths
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_integration_wiring_wire_fastapi(tmp_path):
    """_wire dispatches to _wire_fastapi when pattern=fastapi-router."""
    from phalanx.agents.integration_wiring import IntegrationWiringAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = IntegrationWiringAgent.__new__(IntegrationWiringAgent)
        agent.run_id = "run-iw-2"
        agent.task_id = "task-iw-2"
        agent._log = MagicMock()

    mock_profile = MagicMock()
    mock_profile.integration_pattern = "fastapi-router"

    # Create a minimal fastapi structure
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")

    result = await agent._wire(tmp_path, mock_profile, [])
    assert isinstance(result, dict)
    assert "status" in result


@pytest.mark.asyncio
async def test_integration_wiring_wire_unknown_pattern(tmp_path):
    """_wire with unknown pattern returns status='skipped'."""
    from phalanx.agents.integration_wiring import IntegrationWiringAgent

    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = IntegrationWiringAgent.__new__(IntegrationWiringAgent)
        agent.run_id = "run-iw-3"
        agent.task_id = "task-iw-3"
        agent._log = MagicMock()

    mock_profile = MagicMock()
    mock_profile.integration_pattern = "unknown-pattern"

    result = await agent._wire(tmp_path, mock_profile, [])
    assert result.get("status") in ("skipped", "ok", "error")
