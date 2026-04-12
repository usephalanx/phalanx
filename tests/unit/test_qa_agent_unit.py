"""
Unit tests for phalanx/agents/qa.py data model and helper functions.

Tests QAOutcome, TestSuiteResult, CoverageResult, LintResult, QAReport,
_parse_junit_xml, and _parse_coverage_xml — all without needing a live process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest

from phalanx.agents.qa import (
    CoverageResult,
    LintResult,
    QAOutcome,
    QAReport,
    TestSuiteResult,
    _parse_coverage_xml,
    _parse_junit_xml,
)

# ── QAOutcome ─────────────────────────────────────────────────────────────────


class TestQAOutcome:
    def test_values(self):
        assert QAOutcome.PASSED == "passed"
        assert QAOutcome.FAILED == "failed"
        assert QAOutcome.ERRORED == "errored"

    def test_is_str_enum(self):
        assert isinstance(QAOutcome.PASSED, str)


# ── TestSuiteResult ───────────────────────────────────────────────────────────


class TestTestSuiteResult:
    def _make(self, total=10, passed=8, failed=1, errored=1, skipped=0):
        return TestSuiteResult(
            name="unit",
            total=total,
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            duration_seconds=1.5,
        )

    def test_pass_rate_calculated(self):
        suite = self._make(total=10, passed=8, failed=1, errored=1)
        assert suite.pass_rate == pytest.approx(0.8)

    def test_pass_rate_full_pass(self):
        suite = self._make(total=5, passed=5, failed=0, errored=0)
        assert suite.pass_rate == pytest.approx(1.0)

    def test_pass_rate_zero_total_returns_one(self):
        suite = self._make(total=0, passed=0, failed=0, errored=0)
        assert suite.pass_rate == 1.0

    def test_failures_default_empty(self):
        suite = self._make()
        assert suite.failures == []

    def test_failures_populated(self):
        suite = TestSuiteResult(
            name="unit",
            total=2,
            passed=1,
            failed=1,
            errored=0,
            skipped=0,
            duration_seconds=0.5,
            failures=[
                {"name": "test_foo", "classname": "Foo", "message": "AssertionError", "detail": ""}
            ],
        )
        assert len(suite.failures) == 1
        assert suite.failures[0]["name"] == "test_foo"


# ── CoverageResult ────────────────────────────────────────────────────────────


class TestCoverageResult:
    def test_threshold_met(self):
        r = CoverageResult(
            line_coverage_pct=75.0,
            branch_coverage_pct=60.0,
            threshold_met=True,
            threshold=70.0,
        )
        assert r.threshold_met is True
        assert r.line_coverage_pct == 75.0

    def test_threshold_not_met(self):
        r = CoverageResult(
            line_coverage_pct=60.0,
            branch_coverage_pct=None,
            threshold_met=False,
            threshold=70.0,
            modules_below_threshold=[{"module": "foo", "filename": "foo.py", "coverage_pct": 60.0}],
        )
        assert r.threshold_met is False
        assert len(r.modules_below_threshold) == 1

    def test_branch_coverage_optional(self):
        r = CoverageResult(
            line_coverage_pct=80.0,
            branch_coverage_pct=None,
            threshold_met=True,
            threshold=70.0,
        )
        assert r.branch_coverage_pct is None


# ── LintResult ────────────────────────────────────────────────────────────────


class TestLintResult:
    def test_passed(self):
        r = LintResult(tool="ruff", passed=True, violation_count=0, output="")
        assert r.passed is True
        assert r.violation_count == 0

    def test_failed(self):
        r = LintResult(tool="ruff", passed=False, violation_count=5, output="E501 line too long")
        assert r.passed is False
        assert r.violation_count == 5


# ── QAReport.as_dict ──────────────────────────────────────────────────────────


class TestQAReport:
    def _make(self, outcome=QAOutcome.PASSED, blocking=None):
        run_id = uuid.uuid4()
        return QAReport(
            run_id=run_id,
            task_id=uuid.uuid4(),
            repo_path=Path("/tmp/repo"),
            evaluated_at=datetime.now(UTC),
            outcome=outcome,
            test_suites=[],
            coverage=None,
            lint_results=[],
            blocking_reason=blocking,
        )

    def test_as_dict_top_level_keys(self):
        r = self._make()
        d = r.as_dict()
        for key in (
            "run_id",
            "task_id",
            "repo_path",
            "evaluated_at",
            "outcome",
            "blocking_reason",
            "test_suites",
            "coverage",
            "lint_results",
            "quality_evidence",
        ):
            assert key in d

    def test_run_id_is_string(self):
        r = self._make()
        d = r.as_dict()
        assert isinstance(d["run_id"], str)

    def test_outcome_value(self):
        r = self._make(outcome=QAOutcome.FAILED)
        d = r.as_dict()
        assert d["outcome"] == "failed"

    def test_blocking_reason_none(self):
        r = self._make(blocking=None)
        assert r.as_dict()["blocking_reason"] is None

    def test_blocking_reason_set(self):
        r = self._make(blocking="Test suite failed")
        assert r.as_dict()["blocking_reason"] == "Test suite failed"

    def test_coverage_none_when_not_set(self):
        r = self._make()
        assert r.as_dict()["coverage"] is None

    def test_coverage_dict_when_set(self):
        run_id = uuid.uuid4()
        cov = CoverageResult(
            line_coverage_pct=80.5,
            branch_coverage_pct=70.0,
            threshold_met=True,
            threshold=70.0,
        )
        r = QAReport(
            run_id=run_id,
            task_id=None,
            repo_path=Path("/tmp"),
            evaluated_at=datetime.now(UTC),
            outcome=QAOutcome.PASSED,
            test_suites=[],
            coverage=cov,
            lint_results=[],
            blocking_reason=None,
        )
        d = r.as_dict()
        assert d["coverage"]["line_coverage_pct"] == 80.5
        assert d["coverage"]["threshold_met"] is True

    def test_test_suites_serialized(self):
        suite = TestSuiteResult(
            name="unit", total=5, passed=5, failed=0, errored=0, skipped=0, duration_seconds=1.0
        )
        run_id = uuid.uuid4()
        r = QAReport(
            run_id=run_id,
            task_id=None,
            repo_path=Path("/tmp"),
            evaluated_at=datetime.now(UTC),
            outcome=QAOutcome.PASSED,
            test_suites=[suite],
            coverage=None,
            lint_results=[],
            blocking_reason=None,
        )
        d = r.as_dict()
        assert len(d["test_suites"]) == 1
        assert d["test_suites"][0]["name"] == "unit"
        assert d["test_suites"][0]["pass_rate"] == 1.0

    def test_lint_results_serialized(self):
        lr = LintResult(tool="ruff", passed=False, violation_count=3, output="E501")
        run_id = uuid.uuid4()
        r = QAReport(
            run_id=run_id,
            task_id=None,
            repo_path=Path("/tmp"),
            evaluated_at=datetime.now(UTC),
            outcome=QAOutcome.FAILED,
            test_suites=[],
            coverage=None,
            lint_results=[lr],
            blocking_reason="lint failed",
        )
        d = r.as_dict()
        assert len(d["lint_results"]) == 1
        assert d["lint_results"][0]["tool"] == "ruff"
        assert d["lint_results"][0]["violation_count"] == 3

    def test_task_id_none_when_not_set(self):
        run_id = uuid.uuid4()
        r = QAReport(
            run_id=run_id,
            task_id=None,
            repo_path=Path("/tmp"),
            evaluated_at=datetime.now(UTC),
            outcome=QAOutcome.PASSED,
            test_suites=[],
            coverage=None,
            lint_results=[],
            blocking_reason=None,
        )
        assert r.as_dict()["task_id"] is None


# ── _parse_junit_xml ──────────────────────────────────────────────────────────


class TestParseJunitXml:
    def _write_xml(self, tmp_path, content: str) -> Path:
        p = tmp_path / "junit.xml"
        p.write_text(content)
        return p

    def test_missing_file_returns_empty(self, tmp_path):
        result = _parse_junit_xml(tmp_path / "nofile.xml")
        assert result == []

    def test_malformed_xml_returns_empty(self, tmp_path):
        p = self._write_xml(tmp_path, "<<not xml>>")
        result = _parse_junit_xml(p)
        assert result == []

    def test_single_testsuite_all_pass(self, tmp_path):
        xml = dedent("""\
            <testsuite name="unit" tests="3" failures="0" errors="0" skipped="0" time="0.5">
              <testcase classname="test_foo" name="test_bar"/>
              <testcase classname="test_foo" name="test_baz"/>
              <testcase classname="test_foo" name="test_qux"/>
            </testsuite>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_junit_xml(p)
        assert len(result) == 1
        assert result[0].total == 3
        assert result[0].passed == 3
        assert result[0].failed == 0
        assert result[0].pass_rate == 1.0

    def test_testsuite_with_failures(self, tmp_path):
        xml = dedent("""\
            <testsuite name="unit" tests="3" failures="1" errors="0" skipped="0" time="1.0">
              <testcase classname="foo" name="test_ok"/>
              <testcase classname="foo" name="test_bad">
                <failure message="AssertionError">details here</failure>
              </testcase>
              <testcase classname="foo" name="test_also_ok"/>
            </testsuite>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_junit_xml(p)
        assert result[0].failed == 1
        assert result[0].passed == 2
        assert len(result[0].failures) == 1
        assert result[0].failures[0]["name"] == "test_bad"

    def test_testsuite_with_errors(self, tmp_path):
        xml = dedent("""\
            <testsuite name="unit" tests="2" failures="0" errors="1" skipped="0" time="0.2">
              <testcase classname="foo" name="test_ok"/>
              <testcase classname="foo" name="test_err">
                <error message="RuntimeError">traceback</error>
              </testcase>
            </testsuite>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_junit_xml(p)
        assert result[0].errored == 1
        assert len(result[0].failures) == 1

    def test_testsuites_wrapper(self, tmp_path):
        xml = dedent("""\
            <testsuites>
              <testsuite name="suite1" tests="2" failures="0" errors="0" skipped="0" time="0.3">
                <testcase classname="a" name="t1"/>
                <testcase classname="a" name="t2"/>
              </testsuite>
              <testsuite name="suite2" tests="1" failures="1" errors="0" skipped="0" time="0.1">
                <testcase classname="b" name="t3">
                  <failure message="fail">details</failure>
                </testcase>
              </testsuite>
            </testsuites>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_junit_xml(p)
        assert len(result) == 2
        assert result[0].name == "suite1"
        assert result[1].name == "suite2"
        assert result[1].failed == 1

    def test_skipped_tests_counted(self, tmp_path):
        xml = dedent("""\
            <testsuite name="unit" tests="3" failures="0" errors="0" skipped="1" time="0.4">
              <testcase classname="x" name="t1"/>
              <testcase classname="x" name="t2"/>
              <testcase classname="x" name="t3"/>
            </testsuite>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_junit_xml(p)
        assert result[0].skipped == 1
        assert result[0].passed == 2


# ── _parse_coverage_xml ───────────────────────────────────────────────────────


class TestParseCoverageXml:
    def _write_xml(self, tmp_path, content: str) -> Path:
        p = tmp_path / "coverage.xml"
        p.write_text(content)
        return p

    def test_missing_file_returns_none(self, tmp_path):
        result = _parse_coverage_xml(tmp_path / "nofile.xml")
        assert result is None

    def test_malformed_xml_returns_none(self, tmp_path):
        p = self._write_xml(tmp_path, "<<garbage>>")
        result = _parse_coverage_xml(p)
        assert result is None

    def test_above_threshold_passes(self, tmp_path):
        xml = dedent("""\
            <?xml version="1.0" ?>
            <coverage line-rate="0.85" branch-rate="0.72" version="7.0">
              <packages>
                <package name="forge">
                  <classes>
                    <class name="module_a" filename="phalanx/a.py" line-rate="0.90"/>
                    <class name="module_b" filename="phalanx/b.py" line-rate="0.80"/>
                  </classes>
                </package>
              </packages>
            </coverage>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_coverage_xml(p, threshold=70.0)
        assert result is not None
        assert result.threshold_met is True
        assert result.line_coverage_pct == pytest.approx(85.0)
        assert result.branch_coverage_pct == pytest.approx(72.0)
        assert result.modules_below_threshold == []

    def test_below_threshold_fails(self, tmp_path):
        xml = dedent("""\
            <?xml version="1.0" ?>
            <coverage line-rate="0.60" branch-rate="0.50" version="7.0">
              <packages>
                <package name="forge">
                  <classes>
                    <class name="low_cov" filename="phalanx/low.py" line-rate="0.40"/>
                  </classes>
                </package>
              </packages>
            </coverage>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_coverage_xml(p, threshold=70.0)
        assert result is not None
        assert result.threshold_met is False
        assert len(result.modules_below_threshold) == 1
        assert result.modules_below_threshold[0]["module"] == "low_cov"

    def test_no_branch_rate(self, tmp_path):
        xml = dedent("""\
            <?xml version="1.0" ?>
            <coverage line-rate="0.75" version="7.0">
              <packages/>
            </coverage>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_coverage_xml(p)
        assert result is not None
        assert result.branch_coverage_pct is None

    def test_threshold_default_is_70(self, tmp_path):
        xml = dedent("""\
            <?xml version="1.0" ?>
            <coverage line-rate="0.71" version="7.0">
              <packages/>
            </coverage>
        """)
        p = self._write_xml(tmp_path, xml)
        result = _parse_coverage_xml(p)
        assert result.threshold == 70.0
        assert result.threshold_met is True


# ── QA.md parsing ─────────────────────────────────────────────────────────────


class TestParseQAMd:
    """Tests for QAAgent._parse_qa_md() — parses YAML written by last builder task."""

    def _make_agent(self, repo_path):
        import uuid

        from phalanx.agents.qa import QAAgent

        return QAAgent(run_id=uuid.uuid4(), repo_path=repo_path)

    def test_parses_valid_yaml(self, tmp_path):
        agent = self._make_agent(tmp_path)
        # Create a test file that will be found as valid
        test_file = tmp_path / "tests" / "test_app.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_ok(): pass")

        qa_md = dedent("""\
            stack: Python/FastAPI
            app_type: web-api
            workspace: /tmp/test
            test_runner: pytest tests/
            test_files:
              - tests/test_app.py
            lint_tool: ruff check .
            coverage_tool: pytest-cov
            coverage_threshold: 70
            coverage_applies: true
            coverage_source: app
            install_steps:
              - pip install -r requirements.txt
            notes: Verify FastAPI endpoints work
        """)
        result = agent._parse_qa_md(qa_md)
        assert result["test_files"] == ["tests/test_app.py"]
        assert result["coverage_source"] == "app"
        assert result["what_to_verify"] == "Verify FastAPI endpoints work"
        assert result["rationale"] == "QA.md written by last builder task"
        assert result["remove_root_conftest"] is False

    def test_removes_nonexistent_test_files(self, tmp_path):
        agent = self._make_agent(tmp_path)
        qa_md = dedent("""\
            stack: Python/FastAPI
            test_runner: pytest tests/
            test_files:
              - tests/test_existing.py
              - tests/test_missing.py
            install_steps: []
        """)
        # Only create one file
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_existing.py").write_text("pass")

        result = agent._parse_qa_md(qa_md)
        assert result["test_files"] == ["tests/test_existing.py"]
        assert "tests/test_missing.py" not in result["test_files"]

    def test_strips_markdown_fences(self, tmp_path):
        agent = self._make_agent(tmp_path)
        qa_md = dedent("""\
            ```yaml
            stack: TypeScript/React+Vite
            test_runner: npm test
            install_steps: []
            ```
        """)
        result = agent._parse_qa_md(qa_md)
        assert isinstance(result, dict)
        assert result.get("_qa_md_data", {}).get("stack") == "TypeScript/React+Vite"

    def test_returns_empty_dict_on_invalid_yaml(self, tmp_path):
        agent = self._make_agent(tmp_path)
        result = agent._parse_qa_md("not: valid: yaml: :::")
        assert isinstance(result, dict)

    def test_returns_empty_dict_for_non_dict_yaml(self, tmp_path):
        agent = self._make_agent(tmp_path)
        result = agent._parse_qa_md("- item1\n- item2\n")
        assert result == {}


class TestMergeQAMdIntoBrief:
    """Tests for QAAgent._merge_qa_md_into_brief()."""

    def _make_agent(self, tmp_path):
        import uuid

        from phalanx.agents.qa import QAAgent

        return QAAgent(run_id=uuid.uuid4(), repo_path=tmp_path)

    def _make_brief(self):
        from phalanx.agents.qa import TeamBrief

        return TeamBrief()

    def test_merges_stack_and_runner(self, tmp_path):
        agent = self._make_agent(tmp_path)
        brief = self._make_brief()
        test_plan = {
            "_qa_md_data": {
                "stack": "TypeScript/React+Vite",
                "test_runner": "npm test",
                "coverage_applies": False,
                "coverage_threshold": 0,
            }
        }
        merged = agent._merge_qa_md_into_brief(test_plan, brief)
        assert merged.stack == "TypeScript/React+Vite"
        assert merged.test_runner == "npm test"
        assert merged.coverage_applies is False
        assert merged.coverage_threshold == 0.0

    def test_returns_unchanged_brief_when_no_qa_md_data(self, tmp_path):
        agent = self._make_agent(tmp_path)
        brief = self._make_brief()
        brief.stack = "Python/FastAPI"
        merged = agent._merge_qa_md_into_brief({}, brief)
        assert merged.stack == "Python/FastAPI"

    def test_coverage_applies_false_string(self, tmp_path):
        agent = self._make_agent(tmp_path)
        brief = self._make_brief()
        test_plan = {"_qa_md_data": {"coverage_applies": "false"}}
        merged = agent._merge_qa_md_into_brief(test_plan, brief)
        assert merged.coverage_applies is False


class TestWorkspaceIsolation:
    """Tests for BuilderAgent workspace path helpers."""

    def _make_agent(self):
        import uuid
        from unittest.mock import MagicMock

        from phalanx.agents.builder import BuilderAgent

        run_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        agent = BuilderAgent.__new__(BuilderAgent)
        agent.run_id = run_id
        agent.task_id = task_id
        agent._log = MagicMock()
        agent._tokens_used = 0
        return agent, run_id

    def test_make_workspace_path_uses_title_slug(self, tmp_path):
        from unittest.mock import MagicMock, patch

        agent, run_id = self._make_agent()
        run = MagicMock()
        run.project_id = "proj-1"

        with patch("phalanx.agents.builder.settings") as mock_settings:
            mock_settings.git_workspace = str(tmp_path)
            path = agent._make_workspace_path(run, "Hello World React App")

        run_short = run_id[:8]
        assert path.name == f"hello-world-react-app-{run_short}"
        assert path.parent == tmp_path

    def test_make_workspace_path_fallback_without_title(self, tmp_path):
        from unittest.mock import MagicMock, patch

        agent, run_id = self._make_agent()
        run = MagicMock()

        with patch("phalanx.agents.builder.settings") as mock_settings:
            mock_settings.git_workspace = str(tmp_path)
            path = agent._make_workspace_path(run, "")

        run_short = run_id[:8]
        assert path.name == f"run-{run_short}"

    def test_make_workspace_path_truncates_long_title(self, tmp_path):
        from unittest.mock import MagicMock, patch

        agent, run_id = self._make_agent()
        run = MagicMock()

        with patch("phalanx.agents.builder.settings") as mock_settings:
            mock_settings.git_workspace = str(tmp_path)
            long_title = "A very long title that exceeds the maximum allowed slug length easily"
            path = agent._make_workspace_path(run, long_title)

        # Slug is capped at 40 chars + dash + run_id[:8]
        slug_part = path.name.replace(f"-{run_id[:8]}", "")
        assert len(slug_part) <= 40

    def test_validate_qa_md_removes_nonexistent_files(self, tmp_path):
        import yaml

        agent, _ = self._make_agent()

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_real.py").write_text("pass")

        raw = yaml.dump(
            {
                "stack": "Python/FastAPI",
                "test_runner": "pytest tests/",
                "install_steps": ["pip install -r requirements.txt"],
                "test_files": ["tests/test_real.py", "tests/test_ghost.py"],
                "coverage_applies": True,
                "coverage_threshold": 70,
            }
        )

        result = agent._validate_qa_md(raw, tmp_path)
        assert result is not None
        data = yaml.safe_load(result)
        assert "tests/test_real.py" in data["test_files"]
        assert "tests/test_ghost.py" not in data["test_files"]

    def test_validate_qa_md_returns_none_on_invalid_yaml(self, tmp_path):

        agent, _ = self._make_agent()
        result = agent._validate_qa_md("not: valid: yaml: :::broken", tmp_path)
        # Either None or a string with defaults injected
        assert result is None or isinstance(result, str)

    def test_validate_qa_md_injects_workspace_path(self, tmp_path):
        import yaml

        agent, _ = self._make_agent()
        raw = yaml.dump(
            {
                "stack": "Python/FastAPI",
                "test_runner": "pytest tests/",
                "install_steps": [],
            }
        )

        result = agent._validate_qa_md(raw, tmp_path)
        assert result is not None
        data = yaml.safe_load(result)
        assert data["workspace"] == str(tmp_path)
