"""
QA Agent — runs the quality pipeline for a completed task/run before it advances
to the ship approval gate.

Responsibilities:
  1. Execute the project's configured test command
  2. Collect and parse test results (JUnit XML)
  3. Evaluate coverage thresholds
  4. Run linting / formatting checks
  5. Produce a structured `test_report` Artifact with `quality_evidence`
  6. Transition the Run to VERIFYING → AWAITING_SHIP_APPROVAL (pass) or FAILED (fail)

The QA agent never writes code. It only evaluates what the builder produced.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class QAOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"  # infra/tooling error, not a test failure


@dataclass
class TestSuiteResult:
    name: str
    total: int
    passed: int
    failed: int
    errored: int
    skipped: int
    duration_seconds: float
    failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return self.passed / self.total


@dataclass
class CoverageResult:
    line_coverage_pct: float
    branch_coverage_pct: float | None
    threshold_met: bool
    threshold: float
    modules_below_threshold: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LintResult:
    tool: str
    passed: bool
    violation_count: int
    output: str


@dataclass
class QAReport:
    run_id: UUID
    task_id: UUID | None
    repo_path: Path
    evaluated_at: datetime
    outcome: QAOutcome
    test_suites: list[TestSuiteResult]
    coverage: CoverageResult | None
    lint_results: list[LintResult]
    blocking_reason: str | None
    quality_evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "run_id": str(self.run_id),
            "task_id": str(self.task_id) if self.task_id else None,
            "repo_path": str(self.repo_path),
            "evaluated_at": self.evaluated_at.isoformat(),
            "outcome": self.outcome,
            "blocking_reason": self.blocking_reason,
            "test_suites": [
                {
                    "name": s.name,
                    "total": s.total,
                    "passed": s.passed,
                    "failed": s.failed,
                    "errored": s.errored,
                    "skipped": s.skipped,
                    "duration_seconds": s.duration_seconds,
                    "pass_rate": s.pass_rate,
                    "failures": s.failures,
                }
                for s in self.test_suites
            ],
            "coverage": (
                {
                    "line_coverage_pct": self.coverage.line_coverage_pct,
                    "branch_coverage_pct": self.coverage.branch_coverage_pct,
                    "threshold_met": self.coverage.threshold_met,
                    "threshold": self.coverage.threshold,
                    "modules_below_threshold": self.coverage.modules_below_threshold,
                }
                if self.coverage
                else None
            ),
            "lint_results": [
                {
                    "tool": lr.tool,
                    "passed": lr.passed,
                    "violation_count": lr.violation_count,
                }
                for lr in self.lint_results
            ],
            "quality_evidence": self.quality_evidence,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _parse_junit_xml(xml_path: Path) -> list[TestSuiteResult]:
    """Parse JUnit XML and return TestSuiteResult list."""
    results: list[TestSuiteResult] = []
    if not xml_path.exists():
        return results

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError:
        return results

    # Handle both <testsuite> at root and <testsuites><testsuite>
    suites = root.findall(".//testsuite") or ([root] if root.tag == "testsuite" else [])

    for suite in suites:
        total = int(suite.get("tests", 0))
        failed = int(suite.get("failures", 0))
        errored = int(suite.get("errors", 0))
        skipped = int(suite.get("skipped", 0))
        passed = total - failed - errored - skipped
        duration = float(suite.get("time", 0.0))

        failures: list[dict[str, str]] = []
        for tc in suite.findall(".//testcase"):
            failure = tc.find("failure")
            error = tc.find("error")
            node = failure if failure is not None else error
            if node is not None:
                failures.append(
                    {
                        "name": tc.get("name", "unknown"),
                        "classname": tc.get("classname", ""),
                        "message": node.get("message", ""),
                        "detail": (node.text or "")[:500],
                    }
                )

        results.append(
            TestSuiteResult(
                name=suite.get("name", "unknown"),
                total=total,
                passed=passed,
                failed=failed,
                errored=errored,
                skipped=skipped,
                duration_seconds=duration,
                failures=failures,
            )
        )

    return results


def _parse_coverage_xml(coverage_xml_path: Path, threshold: float = 70.0) -> CoverageResult | None:
    """Parse coverage.xml (Cobertura format) produced by pytest-cov."""
    if not coverage_xml_path.exists():
        return None

    try:
        tree = ET.parse(coverage_xml_path)
        root = tree.getroot()
    except ET.ParseError:
        return None

    line_rate = float(root.get("line-rate", 0.0)) * 100
    branch_rate_raw = root.get("branch-rate")
    branch_rate = float(branch_rate_raw) * 100 if branch_rate_raw else None

    modules_below: list[dict[str, Any]] = []
    for pkg in root.findall(".//package"):
        for cls in pkg.findall("classes/class"):
            cls_rate = float(cls.get("line-rate", 0.0)) * 100
            if cls_rate < threshold:
                modules_below.append(
                    {
                        "module": cls.get("name", "?"),
                        "filename": cls.get("filename", "?"),
                        "coverage_pct": round(cls_rate, 1),
                    }
                )

    return CoverageResult(
        line_coverage_pct=round(line_rate, 1),
        branch_coverage_pct=round(branch_rate, 1) if branch_rate is not None else None,
        threshold_met=line_rate >= threshold,
        threshold=threshold,
        modules_below_threshold=modules_below,
    )


# ---------------------------------------------------------------------------
# QA Agent
# ---------------------------------------------------------------------------


class QAAgent:
    """
    Executes the QA pipeline for a run/task and produces a QAReport.

    The agent is stateless — it reads from disk and the DB, writes a single
    Artifact, then returns. The Celery task wrapper handles retries.

    Usage (from Celery task):
        agent = QAAgent(run_id=..., task_id=..., repo_path=Path("/forge-repos/proj"))
        report = await agent.evaluate()
    """

    # Defaults — overridden by project.yaml at runtime
    DEFAULT_TEST_CMD = ["pytest", "--tb=short", "-q", "--junit-xml=test-results.xml",
                        "--cov=forge", "--cov-report=xml:coverage.xml"]
    DEFAULT_LINT_CMD = ["ruff", "check", "."]
    DEFAULT_FORMAT_CMD = ["ruff", "format", "--check", "."]
    COVERAGE_THRESHOLD = 70.0
    CRITICAL_MODULE_THRESHOLD = 80.0
    CRITICAL_MODULES = ["forge/db/models.py", "forge/guardrails/", "forge/agents/"]

    def __init__(
        self,
        run_id: UUID,
        repo_path: Path,
        task_id: UUID | None = None,
        test_command: list[str] | None = None,
        coverage_threshold: float | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.repo_path = repo_path
        self.test_command = test_command or self.DEFAULT_TEST_CMD
        self.coverage_threshold = coverage_threshold or self.COVERAGE_THRESHOLD
        self._log = log.bind(run_id=str(run_id), task_id=str(task_id))

    async def evaluate(self) -> QAReport:
        self._log.info("qa_agent.start")

        # Run all checks concurrently
        test_task = asyncio.create_task(self._run_tests())
        lint_task = asyncio.create_task(self._run_linting())

        (junit_path, test_rc), lint_results = await asyncio.gather(test_task, lint_task)

        # Parse test results
        suites = _parse_junit_xml(junit_path)
        total_failures = sum(s.failed + s.errored for s in suites)
        total_tests = sum(s.total for s in suites)

        # Parse coverage
        coverage_xml_path = self.repo_path / "coverage.xml"
        coverage = _parse_coverage_xml(coverage_xml_path, self.coverage_threshold)

        # Evaluate outcome
        outcome, blocking_reason = self._evaluate_outcome(
            test_rc=test_rc,
            total_tests=total_tests,
            total_failures=total_failures,
            coverage=coverage,
            lint_results=lint_results,
        )

        quality_evidence = self._build_evidence(
            suites=suites,
            coverage=coverage,
            lint_results=lint_results,
            outcome=outcome,
        )

        report = QAReport(
            run_id=self.run_id,
            task_id=self.task_id,
            repo_path=self.repo_path,
            evaluated_at=datetime.now(UTC),
            outcome=outcome,
            test_suites=suites,
            coverage=coverage,
            lint_results=lint_results,
            blocking_reason=blocking_reason,
            quality_evidence=quality_evidence,
        )

        await self._persist_artifact(report)
        await self._update_run_status(report)

        self._log.info(
            "qa_agent.done",
            outcome=outcome,
            tests=total_tests,
            failures=total_failures,
            coverage=coverage.line_coverage_pct if coverage else None,
        )

        return report

    async def _run_tests(self) -> tuple[Path, int]:
        """Execute the test suite and return (junit_xml_path, returncode)."""
        junit_path = self.repo_path / "test-results.xml"

        self._log.info("qa_agent.tests.start", cmd=self.test_command)
        rc, stdout, stderr = await _run(self.test_command, cwd=self.repo_path)
        self._log.info("qa_agent.tests.done", rc=rc)

        return junit_path, rc

    async def _run_linting(self) -> list[LintResult]:
        results: list[LintResult] = []

        # ruff check
        rc, stdout, stderr = await _run(self.DEFAULT_LINT_CMD, cwd=self.repo_path)
        violation_count = len([l for l in stdout.splitlines() if l.strip() and not l.startswith("Found")])
        results.append(LintResult(
            tool="ruff-check",
            passed=rc == 0,
            violation_count=violation_count,
            output=stdout[:3000],
        ))

        # ruff format check
        rc, stdout, stderr = await _run(self.DEFAULT_FORMAT_CMD, cwd=self.repo_path)
        results.append(LintResult(
            tool="ruff-format",
            passed=rc == 0,
            violation_count=0 if rc == 0 else 1,
            output=stdout[:1000],
        ))

        return results

    def _evaluate_outcome(
        self,
        test_rc: int,
        total_tests: int,
        total_failures: int,
        coverage: CoverageResult | None,
        lint_results: list[LintResult],
    ) -> tuple[QAOutcome, str | None]:
        reasons: list[str] = []

        if total_tests == 0:
            reasons.append("No tests found — builder must add tests for new code.")
        elif total_failures > 0:
            reasons.append(f"{total_failures} test(s) failed.")

        if coverage and not coverage.threshold_met:
            reasons.append(
                f"Coverage {coverage.line_coverage_pct}% is below threshold {coverage.threshold}%."
            )

        if coverage and coverage.modules_below_threshold:
            critical_failures = [
                m for m in coverage.modules_below_threshold
                if any(c in m["filename"] for c in self.CRITICAL_MODULES)
                and m["coverage_pct"] < self.CRITICAL_MODULE_THRESHOLD
            ]
            if critical_failures:
                names = [m["module"] for m in critical_failures]
                reasons.append(
                    f"Critical modules below {self.CRITICAL_MODULE_THRESHOLD}% coverage: "
                    f"{', '.join(names)}"
                )

        lint_failures = [lr for lr in lint_results if not lr.passed]
        if lint_failures:
            tools = [lr.tool for lr in lint_failures]
            reasons.append(f"Lint failures: {', '.join(tools)}")

        if reasons:
            return QAOutcome.FAILED, " | ".join(reasons)

        return QAOutcome.PASSED, None

    def _build_evidence(
        self,
        suites: list[TestSuiteResult],
        coverage: CoverageResult | None,
        lint_results: list[LintResult],
        outcome: QAOutcome,
    ) -> dict[str, Any]:
        total = sum(s.total for s in suites)
        passed = sum(s.passed for s in suites)
        failed = sum(s.failed + s.errored for s in suites)

        return {
            "gate": "qa",
            "outcome": outcome,
            "timestamp": datetime.now(UTC).isoformat(),
            "summary": {
                "tests_total": total,
                "tests_passed": passed,
                "tests_failed": failed,
                "pass_rate_pct": round(passed / total * 100, 1) if total else 0,
                "coverage_pct": coverage.line_coverage_pct if coverage else None,
                "coverage_threshold": coverage.threshold if coverage else None,
                "coverage_ok": coverage.threshold_met if coverage else None,
                "lint_ok": all(lr.passed for lr in lint_results),
            },
            "failures": [
                f
                for suite in suites
                for f in suite.failures
            ],
            "modules_below_coverage": coverage.modules_below_threshold if coverage else [],
        }

    async def _persist_artifact(self, report: QAReport) -> None:
        try:
            import hashlib  # noqa: PLC0415

            from sqlalchemy import select  # noqa: PLC0415

            from forge.db.models import Artifact, Run  # noqa: PLC0415
            from forge.db.session import get_db  # noqa: PLC0415

            json_bytes = json.dumps(report.as_dict()).encode()
            content_hash = hashlib.sha256(json_bytes).hexdigest()

            async with get_db() as session:
                row = await session.execute(
                    select(Run.project_id).where(Run.id == str(self.run_id))
                )
                project_id = row.scalar_one()

                artifact = Artifact(
                    run_id=str(self.run_id),
                    task_id=str(self.task_id) if self.task_id else None,
                    project_id=project_id,
                    artifact_type="test_report",
                    title=f"qa_report_{self.run_id}",
                    s3_key=f"local/{self.run_id}/qa_report.json",
                    content_hash=content_hash,
                    quality_evidence=report.as_dict(),
                )
                session.add(artifact)
                await session.commit()
        except Exception as exc:
            self._log.warning("qa_agent.persist_failed", error=str(exc))

    async def _update_run_status(self, report: QAReport) -> None:
        """Transition Run to AWAITING_SHIP_APPROVAL (pass) or FAILED (fail)."""
        try:
            from sqlalchemy import update  # noqa: PLC0415

            from forge.db.models import Run  # noqa: PLC0415
            from forge.db.session import get_db  # noqa: PLC0415

            if report.outcome == QAOutcome.PASSED:
                new_status = "AWAITING_SHIP_APPROVAL"
            else:
                new_status = "FAILED"

            async with get_db() as session:
                await session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(
                        status=new_status,
                        updated_at=datetime.now(UTC),
                    )
                )
                await session.commit()

            self._log.info("qa_agent.run_status_updated", new_status=new_status)
        except Exception as exc:
            self._log.warning("qa_agent.status_update_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


from forge.queue.celery_app import celery_app as _celery  # noqa: E402


@_celery.task(
    name="forge.agents.qa.execute_task",
    bind=True,
    queue="qa",
    max_retries=2,
    acks_late=True,
)
def execute_task(
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """
    Celery entry point: run the QA pipeline for a single task.

    Resolves the workspace path from Run.active_branch / project config,
    delegates to QAAgent.evaluate(), and updates Task.status.
    """
    import asyncio  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy import select, update  # noqa: PLC0415

    from forge.config.settings import get_settings  # noqa: PLC0415
    from forge.db.models import Run, Task  # noqa: PLC0415
    from forge.db.session import get_db  # noqa: PLC0415

    _settings = get_settings()

    async def _run():
        # Load task and run
        async with get_db() as session:
            task_result = await session.execute(select(Task).where(Task.id == task_id))
            task = task_result.scalar_one_or_none()
            if task is None:
                return {"success": False, "error": f"Task {task_id} not found"}

            run_result = await session.execute(select(Run).where(Run.id == run_id))
            run = run_result.scalar_one()

        workspace = (
            Path(_settings.git_workspace) / run.project_id / run_id
        )

        agent = QAAgent(
            run_id=run.id,
            task_id=task.id,
            repo_path=workspace,
        )

        try:
            report = await agent.evaluate()
            outcome = report.outcome

            # Update Task status based on QA outcome
            task_status = "COMPLETED" if outcome == QAOutcome.PASSED else "FAILED"
            async with get_db() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status=task_status,
                        output=report.as_dict(),
                        error=(
                            report.blocking_reason
                            if outcome != QAOutcome.PASSED
                            else None
                        ),
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()

            return {
                "success": outcome == QAOutcome.PASSED,
                "task_id": task_id,
                "run_id": run_id,
                "outcome": str(outcome),
            }

        except Exception as exc:
            log.exception("qa.celery_task_failed", task_id=task_id, error=str(exc))
            async with get_db() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status="FAILED",
                        error=str(exc),
                        failure_count=Task.failure_count + 1,
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
            raise

    result = asyncio.get_event_loop().run_until_complete(_run())
    return result
