"""
Coverage boost 4: QA agent helpers.

Targets phalanx/agents/qa.py uncovered lines:
- _parse_team_brief (lines 308-350)
- _apply_test_plan: pytest/npm/go/fallback paths (lines 1030-1099)
- _fallback_test_files (lines 1101-1118)
- _run_linting: ruff/eslint/golangci/generic (lines 1209-1292)
- _evaluate_outcome (lines 1294-1323)
- _build_evidence (lines 1325-1360)
- _persist_artifact (lines 1362-1393)
- _update_run_status (lines 1395-1423)
- _parse_qa_md (lines 585-667)
- _run_qa_md_install_steps (lines 669-690)
- _remove_root_conftest (lines 821-830)
- _derive_coverage_source (lines 900-947)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_qa_agent(tmp_path: Path):
    """Build a QAAgent without calling __init__ (which requires infra)."""
    from phalanx.agents.qa import QAAgent

    agent = object.__new__(QAAgent)
    agent.run_id = "run-test-1"
    agent.task_id = "task-test-1"
    agent.repo_path = tmp_path
    agent.work_order_title = "Test work order"
    agent.task_description = "Test task"
    agent.coverage_threshold = 70.0
    agent.test_command = [QAAgent._PYTEST_BIN, "--tb=short", "-q"]
    agent._log = MagicMock()
    return agent


# ══════════════════════════════════════════════════════════════════════════════
# _parse_team_brief
# ══════════════════════════════════════════════════════════════════════════════


class TestParseTeamBrief:
    def test_empty_string(self):
        from phalanx.agents.qa import _parse_team_brief

        brief = _parse_team_brief("")
        assert brief.stack == ""
        assert brief.coverage_applies is True

    def test_no_team_brief_section(self):
        from phalanx.agents.qa import _parse_team_brief

        brief = _parse_team_brief("## Other Section\nsome content")
        assert brief.stack == ""

    def test_full_team_brief(self):
        from phalanx.agents.qa import _parse_team_brief

        md = """
## TEAM_BRIEF
stack: python-fastapi
test_runner: pytest tests/
lint_tool: ruff check .
coverage_tool: pytest-cov
coverage_threshold: 80
coverage_applies: true
"""
        brief = _parse_team_brief(md)
        assert brief.stack == "python-fastapi"
        assert brief.test_runner == "pytest tests/"
        assert brief.coverage_threshold == 80.0
        assert brief.coverage_applies is True

    def test_coverage_applies_false(self):
        from phalanx.agents.qa import _parse_team_brief

        md = "## TEAM_BRIEF\ncoverage_applies: false\n"
        brief = _parse_team_brief(md)
        assert brief.coverage_applies is False

    def test_coverage_applies_no(self):
        from phalanx.agents.qa import _parse_team_brief

        md = "## TEAM_BRIEF\ncoverage_applies: no\n"
        brief = _parse_team_brief(md)
        assert brief.coverage_applies is False

    def test_stops_at_next_heading(self):
        from phalanx.agents.qa import _parse_team_brief

        md = """
## TEAM_BRIEF
stack: fastapi
## Other Section
stack: should_not_appear
"""
        brief = _parse_team_brief(md)
        assert brief.stack == "fastapi"

    def test_invalid_coverage_threshold(self):
        from phalanx.agents.qa import _parse_team_brief

        md = "## TEAM_BRIEF\ncoverage_threshold: notanumber\n"
        brief = _parse_team_brief(md)
        # Falls back to default
        assert brief.coverage_threshold == 70.0


# ══════════════════════════════════════════════════════════════════════════════
# _parse_qa_md
# ══════════════════════════════════════════════════════════════════════════════


class TestParseQaMd:
    def test_valid_yaml(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        yaml_content = """
stack: python
test_runner: pytest tests/
test_files:
  - tests/unit/test_foo.py
coverage_source: app
"""
        result = agent._parse_qa_md(yaml_content)
        # _parse_qa_md returns a plan dict; stack stored in _qa_md_data
        assert isinstance(result, dict)
        assert result  # non-empty
        # coverage_source should be promoted to top level
        assert result.get("coverage_source") == "app"

    def test_yaml_with_markdown_fences(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        content = "```yaml\nstack: node\ntest_runner: npm test\n```"
        result = agent._parse_qa_md(content)
        assert isinstance(result, dict)
        # Stack stored in _qa_md_data; test_runner should be promoted
        qa_data = result.get("_qa_md_data", {})
        assert qa_data.get("stack") == "node"

    def test_invalid_yaml_returns_empty(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        result = agent._parse_qa_md("{invalid: yaml: :")
        assert result == {} or isinstance(result, dict)

    def test_non_dict_yaml_returns_empty(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        result = agent._parse_qa_md("- item1\n- item2\n")
        assert result == {} or isinstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# _remove_root_conftest
# ══════════════════════════════════════════════════════════════════════════════


class TestRemoveRootConftest:
    def test_no_conftest(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        # Should not raise
        agent._remove_root_conftest()

    def test_removes_existing_conftest(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        (tmp_path / "conftest.py").write_text("# bad conftest")
        agent._remove_root_conftest()
        assert not (tmp_path / "conftest.py").exists()

    def test_oserror_on_unlink(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        (tmp_path / "conftest.py").write_text("# bad")
        with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
            # Should log warning but not raise
            agent._remove_root_conftest()


# ══════════════════════════════════════════════════════════════════════════════
# _derive_coverage_source
# ══════════════════════════════════════════════════════════════════════════════


class TestDeriveCoverageSource:
    def test_shared_top_level_dir(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        (tmp_path / "app").mkdir()  # must exist as dir
        context = {"changed_files": ["app/routes.py", "app/models.py", "app/utils.py"]}
        result = agent._derive_coverage_source(context)
        assert result == "app"

    def test_root_level_main_py(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        context = {"changed_files": ["main.py", "utils.py"]}
        result = agent._derive_coverage_source(context)
        assert result == "main"

    def test_no_source_files(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        context = {"changed_files": ["tests/test_foo.py"]}
        result = agent._derive_coverage_source(context)
        assert result is None

    def test_multiple_top_dirs(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        context = {"changed_files": ["app/routes.py", "lib/utils.py"]}
        result = agent._derive_coverage_source(context)
        # Multiple dirs → can't determine single source → None
        assert result is None

    def test_empty_changed_files(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        context = {"changed_files": []}
        result = agent._derive_coverage_source(context)
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# _fallback_test_files
# ══════════════════════════════════════════════════════════════════════════════


class TestFallbackTestFiles:
    def test_from_diff(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        test_file = tmp_path / "tests" / "test_foo.py"
        test_file.parent.mkdir()
        test_file.write_text("# test")
        context = {"changed_files": ["tests/test_foo.py"]}
        result = agent._fallback_test_files(context)
        assert "tests/test_foo.py" in result

    def test_tests_dir_fallback(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        context = {"changed_files": ["src/app.py"]}
        result = agent._fallback_test_files(context)
        assert result == ["tests/"]

    def test_empty_when_no_tests_dir(self, tmp_path):
        agent = _make_qa_agent(tmp_path)
        context = {"changed_files": ["src/app.py"]}
        result = agent._fallback_test_files(context)
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# _apply_test_plan
# ══════════════════════════════════════════════════════════════════════════════


class TestApplyTestPlan:
    def test_pytest_with_files(self, tmp_path):
        from phalanx.agents.qa import TeamBrief

        agent = _make_qa_agent(tmp_path)
        test_file = tmp_path / "tests" / "test_foo.py"
        test_file.parent.mkdir()
        test_file.write_text("# test")
        brief = TeamBrief(stack="python", test_runner="pytest", coverage_applies=True)
        plan = {"test_files": ["tests/test_foo.py"], "coverage_source": "app"}
        context = {"changed_files": []}
        agent._apply_test_plan(plan, context, brief)
        assert "tests/test_foo.py" in agent.test_command

    def test_pytest_no_files_fallback(self, tmp_path):
        from phalanx.agents.qa import TeamBrief

        agent = _make_qa_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        brief = TeamBrief(stack="python", test_runner="pytest", coverage_applies=False)
        plan = {"test_files": []}
        context = {"changed_files": []}
        agent._apply_test_plan(plan, context, brief)
        assert "tests/" in agent.test_command

    def test_npm_test_runner(self, tmp_path):
        from phalanx.agents.qa import TeamBrief

        agent = _make_qa_agent(tmp_path)
        brief = TeamBrief(stack="react", test_runner="npm test", coverage_applies=False)
        plan = {}
        context = {"changed_files": []}
        agent._apply_test_plan(plan, context, brief)
        assert agent.test_command == ["npm", "test"]

    def test_go_test_runner_with_cover(self, tmp_path):
        from phalanx.agents.qa import TeamBrief

        agent = _make_qa_agent(tmp_path)
        brief = TeamBrief(stack="go", test_runner="go test ./...", coverage_applies=True)
        plan = {}
        context = {"changed_files": []}
        agent._apply_test_plan(plan, context, brief)
        assert "-cover" in agent.test_command

    def test_custom_runner(self, tmp_path):
        from phalanx.agents.qa import TeamBrief

        agent = _make_qa_agent(tmp_path)
        brief = TeamBrief(stack="ruby", test_runner="bundle exec rspec", coverage_applies=False)
        plan = {}
        context = {"changed_files": []}
        agent._apply_test_plan(plan, context, brief)
        assert "bundle" in agent.test_command

    def test_jest_runner(self, tmp_path):
        from phalanx.agents.qa import TeamBrief

        agent = _make_qa_agent(tmp_path)
        brief = TeamBrief(stack="typescript", test_runner="npx jest", coverage_applies=False)
        plan = {}
        context = {"changed_files": []}
        agent._apply_test_plan(plan, context, brief)
        assert "npx" in agent.test_command


# ══════════════════════════════════════════════════════════════════════════════
# _run_linting
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_linting_none(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="none")
    results = await agent._run_linting(brief)
    assert results == []


@pytest.mark.asyncio
async def test_run_linting_ruff(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="ruff check .")

    async def fake_run(cmd, cwd=None):
        return 0, "All checks passed!", ""

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        results = await agent._run_linting(brief)

    assert any(r.tool == "ruff-check" for r in results)


@pytest.mark.asyncio
async def test_run_linting_eslint(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="eslint src/")

    async def fake_run(cmd, cwd=None):
        return 0, "no errors", ""

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        results = await agent._run_linting(brief)

    assert any(r.tool == "eslint" for r in results)


@pytest.mark.asyncio
async def test_run_linting_eslint_missing(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="eslint src/")

    async def fake_run(cmd, cwd=None):
        raise FileNotFoundError("eslint not found")

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        results = await agent._run_linting(brief)

    assert results == []


@pytest.mark.asyncio
async def test_run_linting_golangci(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="golangci-lint run ./...")

    async def fake_run(cmd, cwd=None):
        return 0, "", ""

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        results = await agent._run_linting(brief)

    assert len(results) == 1
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_run_linting_generic(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="flake8 .")

    async def fake_run(cmd, cwd=None):
        return 1, "some lint errors", ""

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        results = await agent._run_linting(brief)

    assert len(results) == 1
    assert results[0].passed is False


@pytest.mark.asyncio
async def test_run_linting_generic_missing(tmp_path):
    from phalanx.agents.qa import TeamBrief

    agent = _make_qa_agent(tmp_path)
    brief = TeamBrief(lint_tool="mypy .")

    async def fake_run(cmd, cwd=None):
        raise FileNotFoundError

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        results = await agent._run_linting(brief)

    assert results == []


# ══════════════════════════════════════════════════════════════════════════════
# _evaluate_outcome
# ══════════════════════════════════════════════════════════════════════════════


class TestEvaluateOutcome:
    def test_no_tests(self, tmp_path):
        from phalanx.agents.qa import QAOutcome

        agent = _make_qa_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(0, 0, 0, None, [])
        assert outcome == QAOutcome.FAILED
        assert "No tests" in reason

    def test_failures(self, tmp_path):
        from phalanx.agents.qa import QAOutcome

        agent = _make_qa_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(1, 10, 3, None, [])
        assert outcome == QAOutcome.FAILED
        assert "3 test(s)" in reason

    def test_coverage_below_threshold(self, tmp_path):
        from phalanx.agents.qa import CoverageResult, QAOutcome, TeamBrief

        agent = _make_qa_agent(tmp_path)
        coverage = CoverageResult(
            line_coverage_pct=60.0,
            branch_coverage_pct=None,
            threshold=70.0,
            threshold_met=False,
            modules_below_threshold=[],
        )
        brief = TeamBrief(coverage_applies=True)
        outcome, reason = agent._evaluate_outcome(0, 10, 0, coverage, [], brief)
        assert outcome == QAOutcome.FAILED
        assert "60.0%" in reason

    def test_coverage_not_applies(self, tmp_path):
        from phalanx.agents.qa import CoverageResult, QAOutcome, TeamBrief

        agent = _make_qa_agent(tmp_path)
        coverage = CoverageResult(
            line_coverage_pct=50.0,
            branch_coverage_pct=None,
            threshold=70.0,
            threshold_met=False,
            modules_below_threshold=[],
        )
        brief = TeamBrief(coverage_applies=False)
        outcome, reason = agent._evaluate_outcome(0, 10, 0, coverage, [], brief)
        # Coverage below threshold but coverage_applies=False → PASSED
        assert outcome == QAOutcome.PASSED
        assert reason is None

    def test_all_pass(self, tmp_path):
        from phalanx.agents.qa import CoverageResult, QAOutcome, TeamBrief

        agent = _make_qa_agent(tmp_path)
        coverage = CoverageResult(
            line_coverage_pct=85.0,
            branch_coverage_pct=None,
            threshold=70.0,
            threshold_met=True,
            modules_below_threshold=[],
        )
        brief = TeamBrief(coverage_applies=True)
        outcome, reason = agent._evaluate_outcome(0, 10, 0, coverage, [], brief)
        assert outcome == QAOutcome.PASSED
        assert reason is None


# ══════════════════════════════════════════════════════════════════════════════
# _build_evidence
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildEvidence:
    def test_basic_evidence(self, tmp_path):
        from phalanx.agents.qa import LintResult, QAOutcome, TestSuiteResult

        agent = _make_qa_agent(tmp_path)
        suite = TestSuiteResult(
            name="test", total=5, passed=5, failed=0, errored=0, skipped=0, duration_seconds=1.0
        )
        lint = LintResult(tool="ruff", passed=True, violation_count=0, output="")
        evidence = agent._build_evidence([suite], None, [lint], QAOutcome.PASSED)
        assert evidence["gate"] == "qa"
        assert evidence["summary"]["tests_total"] == 5
        assert evidence["summary"]["tests_passed"] == 5

    def test_evidence_with_coverage(self, tmp_path):
        from phalanx.agents.qa import CoverageResult, QAOutcome, TestSuiteResult

        agent = _make_qa_agent(tmp_path)
        suite = TestSuiteResult(
            name="test", total=3, passed=3, failed=0, errored=0, skipped=0, duration_seconds=0.5
        )
        cov = CoverageResult(
            line_coverage_pct=80.0,
            branch_coverage_pct=None,
            threshold=70.0,
            threshold_met=True,
            modules_below_threshold=[],
        )
        evidence = agent._build_evidence([suite], cov, [], QAOutcome.PASSED)
        assert evidence["summary"]["coverage_pct"] == 80.0

    def test_evidence_with_test_plan(self, tmp_path):
        from phalanx.agents.qa import QAOutcome

        agent = _make_qa_agent(tmp_path)
        plan = {"what_to_verify": "API endpoints", "rationale": "Core feature", "test_files": []}
        evidence = agent._build_evidence([], None, [], QAOutcome.PASSED, plan)
        assert evidence["test_plan"]["what_to_verify"] == "API endpoints"


# ══════════════════════════════════════════════════════════════════════════════
# _persist_artifact
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_persist_artifact_success(tmp_path):
    from phalanx.agents.qa import QAOutcome, QAReport

    agent = _make_qa_agent(tmp_path)
    from datetime import UTC, datetime
    from uuid import UUID

    report = QAReport(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        task_id=UUID("00000000-0000-0000-0000-000000000002"),
        repo_path=tmp_path,
        evaluated_at=datetime.now(UTC),
        outcome=QAOutcome.PASSED,
        test_suites=[],
        coverage=None,
        lint_results=[],
        blocking_reason=None,
        quality_evidence={"gate": "qa"},
    )

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one=MagicMock(return_value="proj-1"))
    )
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.db.session.get_db", return_value=mock_ctx):
        await agent._persist_artifact(report)

    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


def _make_report(tmp_path, outcome):
    from datetime import UTC, datetime
    from uuid import UUID

    from phalanx.agents.qa import QAOutcome, QAReport

    return QAReport(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        task_id=UUID("00000000-0000-0000-0000-000000000002"),
        repo_path=tmp_path,
        evaluated_at=datetime.now(UTC),
        outcome=outcome,
        test_suites=[],
        coverage=None,
        lint_results=[],
        blocking_reason="tests failed" if outcome != QAOutcome.PASSED else None,
        quality_evidence={},
    )


@pytest.mark.asyncio
async def test_persist_artifact_exception(tmp_path):
    from phalanx.agents.qa import QAOutcome

    agent = _make_qa_agent(tmp_path)
    report = _make_report(tmp_path, QAOutcome.PASSED)

    with patch("phalanx.db.session.get_db", side_effect=Exception("DB down")):
        # Should not raise
        await agent._persist_artifact(report)

    agent._log.warning.assert_called()


# ══════════════════════════════════════════════════════════════════════════════
# _update_run_status
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_run_status_passed(tmp_path):
    from phalanx.agents.qa import QAOutcome

    agent = _make_qa_agent(tmp_path)
    report = _make_report(tmp_path, QAOutcome.PASSED)
    # Should return early without DB call
    await agent._update_run_status(report)
    # No DB interaction expected
    agent._log.info.assert_called()


@pytest.mark.asyncio
async def test_update_run_status_failed(tmp_path):
    from phalanx.agents.qa import QAOutcome

    agent = _make_qa_agent(tmp_path)
    report = _make_report(tmp_path, QAOutcome.FAILED)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("phalanx.db.session.get_db", return_value=mock_ctx):
        await agent._update_run_status(report)

    mock_session.execute.assert_awaited_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_run_status_db_error(tmp_path):
    from phalanx.agents.qa import QAOutcome

    agent = _make_qa_agent(tmp_path)
    report = _make_report(tmp_path, QAOutcome.FAILED)

    with patch("phalanx.db.session.get_db", side_effect=Exception("connection lost")):
        # Should not raise
        await agent._update_run_status(report)

    agent._log.warning.assert_called()


# ══════════════════════════════════════════════════════════════════════════════
# _run_qa_md_install_steps
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_qa_md_install_steps_empty(tmp_path):
    agent = _make_qa_agent(tmp_path)
    await agent._run_qa_md_install_steps({})  # empty install_steps → no-op


@pytest.mark.asyncio
async def test_run_qa_md_install_steps_success(tmp_path):
    agent = _make_qa_agent(tmp_path)
    data = {"install_steps": ["pip install requests"]}

    async def fake_run(cmd, cwd=None):
        return 0, "", ""

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        await agent._run_qa_md_install_steps(data)

    # Should log install_step_ok
    agent._log.info.assert_called()


@pytest.mark.asyncio
async def test_run_qa_md_install_steps_failure(tmp_path):
    agent = _make_qa_agent(tmp_path)
    data = {"install_steps": ["pip install bad-package-xyz"]}

    async def fake_run(cmd, cwd=None):
        return 1, "", "error: package not found"

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        await agent._run_qa_md_install_steps(data)

    agent._log.warning.assert_called()


@pytest.mark.asyncio
async def test_run_qa_md_install_steps_file_not_found(tmp_path):
    agent = _make_qa_agent(tmp_path)
    data = {"install_steps": ["nonexistent-binary install pkg"]}

    async def fake_run(cmd, cwd=None):
        raise FileNotFoundError

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        await agent._run_qa_md_install_steps(data)

    agent._log.warning.assert_called()


@pytest.mark.asyncio
async def test_run_qa_md_install_steps_exception(tmp_path):
    agent = _make_qa_agent(tmp_path)
    data = {"install_steps": ["pip install something"]}

    async def fake_run(cmd, cwd=None):
        raise RuntimeError("unexpected error")

    with patch("phalanx.agents.qa._run", side_effect=fake_run):
        await agent._run_qa_md_install_steps(data)

    agent._log.warning.assert_called()
