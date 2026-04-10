"""
Comprehensive tests for phalanx/agents/qa.py

Coverage matrix:
  Unit tests (no I/O):
    - _derive_coverage_source: package dir, root file, priority, no source, mixed
    - _apply_test_plan: llm files valid, llm files missing, fallback to diff,
                        fallback to tests/, coverage source from llm, derived,
                        fallback to dot, full pytest command structure
    - _remove_root_conftest: exists→deleted, missing→no-op, oserror handled
    - _evaluate_outcome: pass, no tests, failures, low coverage, lint failure,
                         multiple reasons combined
    - _build_evidence: includes test_plan, no test_plan, coverage fields
    - _read_doc: exists, missing, truncated at max_chars
    - _parse_junit_xml / _parse_coverage_xml: valid, malformed, missing

  Simulation tests (real temp filesystem, mocked subprocess + Claude):
    - happy_path: Claude returns good plan → correct pytest cmd → rc=0 → PASSED
    - tests_pass_but_coverage_low: rc=0 but coverage.xml shows 20% → FAILED
    - tests_fail: rc=1, junit has failures → FAILED with reason
    - no_tests_found: Claude returns [] test_files, fallback to diff works
    - claude_fails: Claude raises exception → fallback diff-based scoping
    - broken_conftest_removed: remove_root_conftest=True → file deleted before pytest
    - coverage_source_from_llm: --cov=app in final command
    - coverage_source_derived: no llm source, app/ package in diff → --cov=app
    - coverage_source_root_file: no package, main.py in diff → --cov=main
    - full_evaluate_passes_and_updates_run_status: DB update to AWAITING_SHIP_APPROVAL
    - full_evaluate_fails_and_updates_run_status: DB update to FAILED
"""

from __future__ import annotations

import asyncio
import textwrap
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call
import xml.etree.ElementTree as ET

import pytest

from phalanx.agents.qa import (
    QAAgent,
    QAOutcome,
    QAReport,
    CoverageResult,
    LintResult,
    TestSuiteResult,
    TeamBrief,
    _parse_junit_xml,
    _parse_coverage_xml,
    _parse_team_brief,
)

# Default Python TeamBrief used in tests that don't care about stack
_PYTHON_BRIEF = TeamBrief(
    stack="Python/FastAPI",
    test_runner="pytest tests/",
    lint_tool="ruff check .",
    coverage_tool="pytest-cov",
    coverage_threshold=70.0,
    coverage_applies=True,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

RUN_ID = uuid.uuid4()
TASK_ID = uuid.uuid4()


def make_agent(tmp_path: Path, **kwargs) -> QAAgent:
    return QAAgent(
        run_id=RUN_ID,
        task_id=TASK_ID,
        repo_path=tmp_path,
        task_description=kwargs.get("task_description", "Build a hello world API"),
        work_order_title=kwargs.get("work_order_title", "hello world api"),
        coverage_threshold=kwargs.get("coverage_threshold", 70.0),
    )


def write_junit_xml(path: Path, tests: int = 5, failures: int = 0, errors: int = 0) -> None:
    cases = ""
    for i in range(failures):
        cases += f'<testcase name="test_fail_{i}"><failure message="assert False">detail</failure></testcase>'
    for i in range(errors):
        cases += f'<testcase name="test_error_{i}"><error message="RuntimeError">trace</error></testcase>'
    for i in range(tests - failures - errors):
        cases += f'<testcase name="test_pass_{i}"/>'
    xml = f'<?xml version="1.0"?><testsuite name="pytest" tests="{tests}" failures="{failures}" errors="{errors}" skipped="0" time="1.0">{cases}</testsuite>'
    path.write_text(xml)


def write_coverage_xml(path: Path, line_rate: float = 0.95) -> None:
    xml = textwrap.dedent(f"""\
        <?xml version="1.0"?>
        <coverage version="7.0" line-rate="{line_rate}" branch-rate="0.8">
          <packages>
            <package name="app">
              <classes>
                <class name="main.py" filename="app/main.py" line-rate="{line_rate}"/>
              </classes>
            </package>
          </packages>
        </coverage>
    """)
    path.write_text(xml)


def write_broken_conftest(path: Path) -> None:
    path.write_text(textwrap.dedent("""\
        def pytest_addoption(parser):
            try:
                parser.addoption("--timeout", action="store", default=None)
            except ValueError:
                pass
    """))


# ── Unit: _derive_coverage_source ────────────────────────────────────────────

class TestDeriveCoverageSource:
    def test_package_dir_detected(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "app").mkdir()
        context = {"changed_files": ["app/__init__.py", "app/main.py", "tests/test_main.py"]}
        assert agent._derive_coverage_source(context) == "app"

    def test_root_main_py_priority(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": ["main.py", "requirements.txt"]}
        assert agent._derive_coverage_source(context) == "main"

    def test_root_app_py_priority(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": ["app.py", "server.py"]}
        assert agent._derive_coverage_source(context) == "app"

    def test_root_server_py(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": ["server.py"]}
        assert agent._derive_coverage_source(context) == "server"

    def test_no_source_files_returns_none(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": ["README.md", "RUNNING.md", "Dockerfile"]}
        assert agent._derive_coverage_source(context) is None

    def test_only_test_files_returns_none(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": ["tests/test_main.py", "tests/__init__.py"]}
        assert agent._derive_coverage_source(context) is None

    def test_multiple_packages_returns_none(self, tmp_path):
        # Ambiguous — two top-level packages changed
        agent = make_agent(tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "api").mkdir()
        context = {"changed_files": ["app/main.py", "api/routes.py"]}
        assert agent._derive_coverage_source(context) is None

    def test_package_dir_must_exist_on_disk(self, tmp_path):
        agent = make_agent(tmp_path)
        # "app" in diff but not on disk
        context = {"changed_files": ["app/main.py"]}
        assert agent._derive_coverage_source(context) is None

    def test_ignores_config_files(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": ["conftest.py", "setup.py", "main.py"]}
        assert agent._derive_coverage_source(context) == "main"

    def test_empty_changed_files(self, tmp_path):
        agent = make_agent(tmp_path)
        context = {"changed_files": []}
        assert agent._derive_coverage_source(context) is None


# ── Unit: _apply_test_plan ────────────────────────────────────────────────────

class TestApplyTestPlan:
    def test_llm_files_used_when_valid(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("")
        test_plan = {"test_files": ["tests/test_main.py"], "coverage_source": "app"}
        context = {"changed_files": [], "existing_test_files": []}
        agent._apply_test_plan(test_plan, context, _PYTHON_BRIEF)
        assert "tests/test_main.py" in agent.test_command
        assert "--cov=app" in agent.test_command

    def test_invalid_llm_files_falls_back_to_diff(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_real.py").write_text("")
        test_plan = {"test_files": ["tests/ghost_file.py"]}
        context = {"changed_files": ["tests/test_real.py", "app/main.py"], "existing_test_files": []}
        agent._apply_test_plan(test_plan, context, _PYTHON_BRIEF)
        assert "tests/test_real.py" in agent.test_command
        assert "tests/ghost_file.py" not in agent.test_command

    def test_no_llm_and_no_diff_falls_back_to_tests_dir(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_something.py").write_text("")
        test_plan = {"test_files": []}
        context = {"changed_files": [], "existing_test_files": []}
        agent._apply_test_plan(test_plan, context, _PYTHON_BRIEF)
        assert "tests/" in agent.test_command

    def test_coverage_source_from_llm(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_api.py").write_text("")
        test_plan = {"test_files": ["tests/test_api.py"], "coverage_source": "api"}
        context = {"changed_files": [], "existing_test_files": []}
        agent._apply_test_plan(test_plan, context, _PYTHON_BRIEF)
        assert "--cov=api" in agent.test_command
        assert "--cov=." not in agent.test_command

    def test_coverage_source_derived_when_llm_missing(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("")
        test_plan = {"test_files": ["tests/test_main.py"]}  # no coverage_source
        context = {"changed_files": ["app/main.py"], "existing_test_files": []}
        agent._apply_test_plan(test_plan, context, _PYTHON_BRIEF)
        assert "--cov=app" in agent.test_command

    def test_coverage_fallback_to_dot_when_no_source_found(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("")
        test_plan = {"test_files": ["tests/test_x.py"]}
        context = {"changed_files": ["README.md"], "existing_test_files": []}
        agent._apply_test_plan(test_plan, context, _PYTHON_BRIEF)
        assert "--cov=." in agent.test_command

    def test_pytest_bin_always_first(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("")
        agent._apply_test_plan(
            {"test_files": ["tests/test_x.py"], "coverage_source": "app"},
            {"changed_files": [], "existing_test_files": []},
            _PYTHON_BRIEF,
        )
        assert agent.test_command[0] == agent._PYTEST_BIN

    def test_junit_xml_flag_preserved(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("")
        agent._apply_test_plan(
            {"test_files": ["tests/test_x.py"], "coverage_source": "app"},
            {"changed_files": [], "existing_test_files": []},
            _PYTHON_BRIEF,
        )
        assert "--junit-xml=test-results.xml" in agent.test_command

    def test_no_duplicate_cov_flags(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("")
        agent._apply_test_plan(
            {"test_files": ["tests/test_x.py"], "coverage_source": "app"},
            {"changed_files": [], "existing_test_files": []},
            _PYTHON_BRIEF,
        )
        cov_flags = [f for f in agent.test_command if f.startswith("--cov=")]
        assert len(cov_flags) == 1


# ── Unit: _remove_root_conftest ───────────────────────────────────────────────

class TestRemoveRootConftest:
    def test_removes_existing_conftest(self, tmp_path):
        agent = make_agent(tmp_path)
        write_broken_conftest(tmp_path / "conftest.py")
        agent._remove_root_conftest()
        assert not (tmp_path / "conftest.py").exists()

    def test_no_error_when_missing(self, tmp_path):
        agent = make_agent(tmp_path)
        agent._remove_root_conftest()  # should not raise

    def test_oserror_handled_gracefully(self, tmp_path):
        agent = make_agent(tmp_path)
        conftest = tmp_path / "conftest.py"
        write_broken_conftest(conftest)
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            agent._remove_root_conftest()  # must not raise


# ── Unit: _evaluate_outcome ───────────────────────────────────────────────────

class TestEvaluateOutcome:
    def _lint_ok(self):
        return [
            LintResult(tool="ruff-check", passed=True, violation_count=0, output=""),
            LintResult(tool="ruff-format", passed=True, violation_count=0, output=""),
        ]

    def _lint_fail(self):
        return [
            LintResult(tool="ruff-check", passed=False, violation_count=3, output="E501"),
            LintResult(tool="ruff-format", passed=True, violation_count=0, output=""),
        ]

    def _coverage_ok(self):
        return CoverageResult(line_coverage_pct=85.0, branch_coverage_pct=None, threshold_met=True, threshold=70.0)

    def _coverage_low(self):
        return CoverageResult(line_coverage_pct=50.0, branch_coverage_pct=None, threshold_met=False, threshold=70.0)

    def test_all_pass(self, tmp_path):
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=0, total_tests=5, total_failures=0,
            coverage=self._coverage_ok(), lint_results=self._lint_ok(),
        )
        assert outcome == QAOutcome.PASSED
        assert reason is None

    def test_no_tests_fails(self, tmp_path):
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=0, total_tests=0, total_failures=0,
            coverage=None, lint_results=self._lint_ok(),
        )
        assert outcome == QAOutcome.FAILED
        assert "No tests" in reason

    def test_test_failures_fails(self, tmp_path):
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=1, total_tests=5, total_failures=2,
            coverage=self._coverage_ok(), lint_results=self._lint_ok(),
        )
        assert outcome == QAOutcome.FAILED
        assert "2 test(s) failed" in reason

    def test_low_coverage_fails(self, tmp_path):
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=0, total_tests=5, total_failures=0,
            coverage=self._coverage_low(), lint_results=self._lint_ok(),
        )
        assert outcome == QAOutcome.FAILED
        assert "50.0%" in reason

    def test_lint_failure_does_not_block(self, tmp_path):
        # Lint is advisory — does NOT fail the QA gate (belongs in Reviewer)
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=0, total_tests=5, total_failures=0,
            coverage=self._coverage_ok(), lint_results=self._lint_fail(),
        )
        assert outcome == QAOutcome.PASSED
        assert reason is None

    def test_multiple_reasons_combined(self, tmp_path):
        # Test failures + low coverage → both blocking, lint advisory only
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=1, total_tests=3, total_failures=1,
            coverage=self._coverage_low(), lint_results=self._lint_fail(),
        )
        assert outcome == QAOutcome.FAILED
        assert " | " in reason  # test failure + coverage, both blocking
        assert "test(s) failed" in reason

    def test_no_coverage_does_not_fail(self, tmp_path):
        agent = make_agent(tmp_path)
        outcome, reason = agent._evaluate_outcome(
            test_rc=0, total_tests=3, total_failures=0,
            coverage=None, lint_results=self._lint_ok(),
        )
        assert outcome == QAOutcome.PASSED


# ── Unit: _build_evidence ────────────────────────────────────────────────────

class TestBuildEvidence:
    def test_includes_test_plan(self, tmp_path):
        agent = make_agent(tmp_path)
        suites = [TestSuiteResult("s", 5, 5, 0, 0, 0, 1.0)]
        evidence = agent._build_evidence(
            suites=suites, coverage=None, lint_results=[], outcome=QAOutcome.PASSED,
            test_plan={"what_to_verify": "GET /", "rationale": "only endpoint", "test_files": ["tests/test_main.py"]},
        )
        assert evidence["test_plan"]["what_to_verify"] == "GET /"
        assert "tests/test_main.py" in evidence["test_plan"]["test_files"]

    def test_no_test_plan_key_absent(self, tmp_path):
        agent = make_agent(tmp_path)
        evidence = agent._build_evidence(
            suites=[], coverage=None, lint_results=[], outcome=QAOutcome.PASSED,
            test_plan=None,
        )
        assert "test_plan" not in evidence

    def test_summary_fields_present(self, tmp_path):
        agent = make_agent(tmp_path)
        coverage = CoverageResult(85.0, None, True, 70.0)
        suites = [TestSuiteResult("s", 10, 9, 1, 0, 0, 2.0)]
        evidence = agent._build_evidence(suites=suites, coverage=coverage, lint_results=[], outcome=QAOutcome.FAILED)
        s = evidence["summary"]
        assert s["tests_total"] == 10
        assert s["tests_passed"] == 9
        assert s["tests_failed"] == 1
        assert s["coverage_pct"] == 85.0
        assert s["pass_rate_pct"] == pytest.approx(90.0)

    def test_gate_always_qa(self, tmp_path):
        agent = make_agent(tmp_path)
        evidence = agent._build_evidence(suites=[], coverage=None, lint_results=[], outcome=QAOutcome.PASSED)
        assert evidence["gate"] == "qa"


# ── Unit: _read_doc ───────────────────────────────────────────────────────────

class TestReadDoc:
    def test_reads_existing_file(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "RUNNING.md").write_text("# Run me\nstart with: python main.py")
        content = agent._read_doc("RUNNING.md")
        assert "Run me" in content

    def test_missing_file_returns_empty(self, tmp_path):
        agent = make_agent(tmp_path)
        assert agent._read_doc("NONEXISTENT.md") == ""

    def test_truncated_at_max_chars(self, tmp_path):
        agent = make_agent(tmp_path)
        (tmp_path / "BIG.md").write_text("x" * 10000)
        content = agent._read_doc("BIG.md", max_chars=100)
        assert len(content) == 100


# ── Simulation: full evaluate() flow ─────────────────────────────────────────

def _fake_run_factory(responses: dict[str, tuple[int, str, str]]):
    """
    Returns an async _run() mock that returns specific (rc, stdout, stderr)
    based on the first element of the command list.
    """
    async def fake_run(cmd, cwd=None):
        key = cmd[0] if cmd else ""
        # Match by any substring in command
        for pattern, response in responses.items():
            if pattern in " ".join(cmd):
                return response
        return (0, "", "")
    return fake_run


GOOD_CLAUDE_RESPONSE = """{
  "test_files": ["tests/test_main.py"],
  "coverage_source": "app",
  "what_to_verify": "GET / returns hello",
  "rationale": "only test file for the app package",
  "remove_root_conftest": false
}"""

BROKEN_CONFTEST_CLAUDE_RESPONSE = """{
  "test_files": ["tests/test_main.py"],
  "coverage_source": "app",
  "what_to_verify": "GET / returns hello",
  "rationale": "test_main covers app",
  "remove_root_conftest": true
}"""


def _setup_happy_workspace(tmp_path: Path) -> None:
    """Create a minimal valid workspace: app package, test file, docs."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text("def hello(): return 'hello'")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_main.py").write_text("def test_hello(): assert True")
    (tmp_path / "RUNNING.md").write_text("# Hello API\npython -m app.main")
    (tmp_path / "requirements.txt").write_text("fastapi\n")


class TestEvaluateSimulation:
    """
    Integration-style tests: real QAAgent.evaluate() but with mocked:
      - subprocess (_run) — no real pytest/ruff executed
      - OpenAI (_call_openai_sync) — returns controlled JSON
      - DB calls (_persist_artifact, _update_run_status) — stubbed out
    """

    def _patch_db(self):
        """Patch both DB methods to no-ops."""
        persist = patch.object(QAAgent, "_persist_artifact", new_callable=lambda: lambda self: AsyncMock(return_value=None))
        update = patch.object(QAAgent, "_update_run_status", new_callable=lambda: lambda self: AsyncMock(return_value=None))
        return persist, update

    @pytest.mark.asyncio
    async def test_happy_path_passes(self, tmp_path):
        _setup_happy_workspace(tmp_path)

        write_junit_xml(tmp_path / "test-results.xml", tests=4, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.95)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                if "--stat" in cmd:
                    return (0, "app/main.py | 10 +++++\ntests/test_main.py | 5 +++++", "")
                if "--name-only" in cmd:
                    return (0, "app/main.py\napp/__init__.py\ntests/test_main.py\n", "")
                return (0, "", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "4 passed", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)

        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        assert report.outcome == QAOutcome.PASSED
        assert report.blocking_reason is None
        assert "--cov=app" in agent.test_command
        assert "tests/test_main.py" in agent.test_command

    @pytest.mark.asyncio
    async def test_tests_fail_outcome_failed(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=4, failures=2)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.90)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (1, "", "2 failed")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        assert report.outcome == QAOutcome.FAILED
        assert "2 test(s) failed" in report.blocking_reason

    @pytest.mark.asyncio
    async def test_low_coverage_fails(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=4, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.20)  # 20%

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "4 passed", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        assert report.outcome == QAOutcome.FAILED
        assert "20.0%" in report.blocking_reason

    @pytest.mark.asyncio
    async def test_broken_conftest_removed_before_tests(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_broken_conftest(tmp_path / "conftest.py")
        write_junit_xml(tmp_path / "test-results.xml", tests=3, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.85)

        pytest_cmds = []

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                pytest_cmds.append(cmd)
                # Verify conftest is gone by this point
                assert not (tmp_path / "conftest.py").exists(), "conftest.py should be removed before pytest runs"
                return (0, "3 passed", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: BROKEN_CONFTEST_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        assert not (tmp_path / "conftest.py").exists()
        assert report.outcome == QAOutcome.PASSED

    @pytest.mark.asyncio
    async def test_claude_failure_falls_back_to_diff(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=2, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.80)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd and "--name-only" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "git" in cmd:
                return (0, "", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "2 passed", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=Exception("API error")),  \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        # Should still work via fallback — diff has test_main.py
        assert "tests/test_main.py" in agent.test_command
        assert report.outcome == QAOutcome.PASSED

    @pytest.mark.asyncio
    async def test_no_test_files_fails_with_clear_message(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        # Remove test files
        (tmp_path / "tests" / "test_main.py").unlink()
        (tmp_path / "tests" / "__init__.py").unlink()
        (tmp_path / "tests").rmdir()

        # No junit xml written — pytest found nothing
        async def fake_run(cmd, cwd=None):
            cmd_str = " ".join(cmd)
            if "git" in cmd_str:
                return (0, "app/main.py\n", "")
            if "pip" in cmd_str:
                return (0, "", "")
            if "pytest" in cmd_str:
                return (1, "no tests ran", "")
            if "ruff" in cmd_str:
                return (0, "", "")
            return (0, "", "")

        no_tests_plan = '{"test_files": [], "coverage_source": "app", "what_to_verify": "nothing", "rationale": "no tests", "remove_root_conftest": false}'
        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: no_tests_plan), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        assert report.outcome == QAOutcome.FAILED
        # Either "No tests found" or "test(s) failed" — both indicate no real tests
        assert report.blocking_reason is not None

    @pytest.mark.asyncio
    async def test_update_run_status_called_with_passing(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=3, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.90)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        update_mock = AsyncMock()
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", update_mock):
            report = await agent.evaluate()

        update_mock.assert_called_once_with(report)
        assert report.outcome == QAOutcome.PASSED

    @pytest.mark.asyncio
    async def test_update_run_status_called_with_failing(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=3, failures=1)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.90)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (1, "", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        update_mock = AsyncMock()
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", update_mock):
            report = await agent.evaluate()

        update_mock.assert_called_once_with(report)
        assert report.outcome == QAOutcome.FAILED

    @pytest.mark.asyncio
    async def test_test_plan_included_in_quality_evidence(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=4, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.90)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        assert "test_plan" in report.quality_evidence
        assert report.quality_evidence["test_plan"]["what_to_verify"] == "GET / returns hello"

    @pytest.mark.asyncio
    async def test_lint_failure_propagates_to_outcome(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=4, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.90)

        async def fake_run(cmd, cwd=None):
            if "git" in cmd:
                return (0, "app/main.py\ntests/test_main.py\n", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "", "")
            if "ruff" in " ".join(cmd) and "check" in cmd:
                return (1, "E501 line too long\nE302 expected 2 blank lines", "")  # lint fail
            if "ruff" in " ".join(cmd):
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=lambda s, m: GOOD_CLAUDE_RESPONSE), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            report = await agent.evaluate()

        # Lint is advisory — does NOT fail the QA gate
        assert report.outcome == QAOutcome.PASSED
        assert report.blocking_reason is None

    @pytest.mark.asyncio
    async def test_coverage_source_derived_from_diff_when_llm_missing(self, tmp_path):
        _setup_happy_workspace(tmp_path)
        write_junit_xml(tmp_path / "test-results.xml", tests=4, failures=0)
        write_coverage_xml(tmp_path / "coverage.xml", line_rate=0.90)

        no_cov_source_plan = '{"test_files": ["tests/test_main.py"], "what_to_verify": "GET /", "rationale": "test covers app", "remove_root_conftest": false}'
        no_cov_source_lambda = lambda s, m: no_cov_source_plan  # noqa: E731

        async def fake_run(cmd, cwd=None):
            if "git" in cmd and "--name-only" in cmd:
                return (0, "app/main.py\napp/__init__.py\ntests/test_main.py\n", "")
            if "git" in cmd:
                return (0, "", "")
            if "pip" in cmd:
                return (0, "", "")
            if "pytest" in cmd:
                return (0, "", "")
            if "ruff" in cmd:
                return (0, "", "")
            return (0, "", "")

        agent = make_agent(tmp_path)
        with patch("phalanx.agents.qa._run", side_effect=fake_run), \
             patch.object(agent, "_call_openai_sync", side_effect=no_cov_source_lambda), \
             patch.object(agent, "_persist_artifact", new=AsyncMock()), \
             patch.object(agent, "_update_run_status", new=AsyncMock()):
            await agent.evaluate()

        # app/ exists on disk and is in diff → should derive "app"
        assert "--cov=app" in agent.test_command


# ── Unit: _parse_junit_xml ────────────────────────────────────────────────────

class TestParseJunitXml:
    def test_valid_xml_parsed(self, tmp_path):
        f = tmp_path / "results.xml"
        write_junit_xml(f, tests=5, failures=1, errors=0)
        suites = _parse_junit_xml(f)
        assert len(suites) == 1
        assert suites[0].total == 5
        assert suites[0].failed == 1

    def test_missing_file_returns_empty(self, tmp_path):
        assert _parse_junit_xml(tmp_path / "ghost.xml") == []

    def test_malformed_xml_returns_empty(self, tmp_path):
        f = tmp_path / "bad.xml"
        f.write_text("<not valid xml <<<")
        assert _parse_junit_xml(f) == []

    def test_failures_captured(self, tmp_path):
        f = tmp_path / "results.xml"
        write_junit_xml(f, tests=2, failures=1)
        suites = _parse_junit_xml(f)
        assert len(suites[0].failures) == 1
        assert "test_fail_0" in suites[0].failures[0]["name"]


# ── Unit: _parse_coverage_xml ────────────────────────────────────────────────

class TestParseCoverageXml:
    def test_high_coverage_passes_threshold(self, tmp_path):
        f = tmp_path / "coverage.xml"
        write_coverage_xml(f, line_rate=0.92)
        result = _parse_coverage_xml(f, threshold=70.0)
        assert result is not None
        assert result.line_coverage_pct == pytest.approx(92.0, abs=0.5)
        assert result.threshold_met is True

    def test_low_coverage_fails_threshold(self, tmp_path):
        f = tmp_path / "coverage.xml"
        write_coverage_xml(f, line_rate=0.40)
        result = _parse_coverage_xml(f, threshold=70.0)
        assert result.threshold_met is False
        assert result.line_coverage_pct == pytest.approx(40.0, abs=0.5)

    def test_missing_file_returns_none(self, tmp_path):
        assert _parse_coverage_xml(tmp_path / "ghost.xml") is None

    def test_malformed_returns_none(self, tmp_path):
        f = tmp_path / "bad.xml"
        f.write_text("not xml")
        assert _parse_coverage_xml(f) is None
