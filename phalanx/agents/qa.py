"""
QA Agent — LLM-driven quality engineer.

Behaves like a real QA engineer:
  1. Reads the branch's RUNNING.md / README / ARCHITECTURE docs
  2. Diffs the branch against origin/main to understand what changed
  3. Asks Claude to produce a test plan: which files to run, what to verify, why
  4. Cleans up any broken conftest stubs from prior runs
  5. Executes the scoped test suite + lint
  6. Evaluates results, produces a structured QAReport artifact
  7. Transitions Run to AWAITING_SHIP_APPROVAL (pass) or FAILED (fail)

The agent never writes code. It reads, thinks, runs, reports.
"""

from __future__ import annotations

import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from uuid import UUID

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
# QA Agent — LLM-driven
# ---------------------------------------------------------------------------

_QA_SYSTEM_PROMPT = """\
You are a senior QA engineer in an AI software delivery pipeline called FORGE.

A builder agent just committed new code to a branch. Your job is to produce a
precise test plan so the QA runner knows exactly what to execute.

## Key principle: follow the TEAM_BRIEF
RUNNING.md contains a ## TEAM_BRIEF section written by the planner.
It defines the stack, test runner, lint tool, coverage tool and threshold.
You MUST follow this brief — do not invent tools or commands.

## Your responsibilities
1. Read RUNNING.md → find the ## TEAM_BRIEF section to understand the stack and tools.
2. Look at which files changed in this branch to identify what was built.
3. Find the test files that cover those changes (only files that actually exist).
4. Determine coverage_source: the package/module/file to measure coverage against.
   - Python: the top-level package (e.g. "app", "main", "api") — NOT "."
   - TypeScript/React: set to null (coverage_applies=false in TEAM_BRIEF means skip)
   - Go: set to "./..."
   - If coverage_applies=false in TEAM_BRIEF, always set coverage_source to null.
5. Detect broken pytest conftest.py: root-level conftest.py that registers --timeout
   via pytest_addoption — this conflicts with pytest-timeout plugin, must be removed.

## Rules
- Only include test files that EXIST in the repository (given full list below).
- Never invent file names.
- Scope to tests that cover THIS branch's changes only — exclude accumulated files
  from prior runs that are unrelated to what was just built.
- If no test files exist, set test_files to [].
- If TEAM_BRIEF is missing from RUNNING.md, infer the stack from file extensions.

## Output format — return ONLY this JSON, no markdown fences:
{
  "test_files": ["tests/test_foo.py"],
  "coverage_source": "app",
  "what_to_verify": "one sentence describing what is being tested",
  "rationale": "brief explanation of which files you chose and why",
  "remove_root_conftest": false
}
"""


@dataclass
class TeamBrief:
    """
    Parsed from the ## TEAM_BRIEF section of RUNNING.md.
    Written by the Planner; read by every agent as shared team context.
    """
    stack: str = ""
    test_runner: str = "pytest tests/"
    lint_tool: str = "ruff check ."
    coverage_tool: str = "pytest-cov"
    coverage_threshold: float = 70.0
    coverage_applies: bool = True


def _parse_team_brief(running_md: str) -> TeamBrief:
    """
    Extract the ## TEAM_BRIEF block from RUNNING.md.
    Returns defaults if section is missing or malformed.
    """
    brief = TeamBrief()
    if not running_md:
        return brief

    # Find ## TEAM_BRIEF section
    lines = running_md.splitlines()
    in_brief = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## team_brief"):
            in_brief = True
            continue
        if in_brief:
            # Stop at next ## heading
            if stripped.startswith("##"):
                break
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if not val:
                continue
            if key == "stack":
                brief.stack = val
            elif key == "test_runner":
                brief.test_runner = val
            elif key == "lint_tool":
                brief.lint_tool = val
            elif key == "coverage_tool":
                brief.coverage_tool = val
            elif key == "coverage_threshold":
                try:
                    brief.coverage_threshold = float(val)
                except ValueError:
                    pass
            elif key == "coverage_applies":
                brief.coverage_applies = val.lower() not in ("false", "no", "0")

    return brief


class QAAgent:
    """
    LLM-driven, skill-based QA engineer.

    Reads the TEAM_BRIEF from RUNNING.md (written by Planner),
    asks Claude which tests to run, then executes using the tools
    defined in the brief. Language-agnostic: works for Python,
    TypeScript/React, Go, Node, static HTML — whatever the stack is.
    """

    _PYTEST_BIN = str(Path(sys.executable).parent / "pytest")
    DEFAULT_TEST_CMD = [
        _PYTEST_BIN,
        "--tb=short",
        "-q",
        "--junit-xml=test-results.xml",
        "--cov=.",
        "--cov-report=xml:coverage.xml",
    ]
    DEFAULT_LINT_CMD = ["ruff", "check", "."]
    DEFAULT_FORMAT_CMD = ["ruff", "format", "--check", "."]
    COVERAGE_THRESHOLD = 70.0

    def __init__(
        self,
        run_id: "UUID",
        repo_path: Path,
        task_id: "UUID | None" = None,
        test_command: list[str] | None = None,
        coverage_threshold: float | None = None,
        task_description: str | None = None,
        work_order_title: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.repo_path = repo_path
        self.test_command = list(test_command or self.DEFAULT_TEST_CMD)
        self.coverage_threshold = coverage_threshold or self.COVERAGE_THRESHOLD
        self.task_description = task_description or ""
        self.work_order_title = work_order_title or ""
        self._log = log.bind(run_id=str(run_id), task_id=str(task_id))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def evaluate(self) -> QAReport:
        self._log.info("qa_agent.start")

        # 1. Gather context: git diff, docs, test files
        context = await self._gather_workspace_context()

        # 2. Parse TEAM_BRIEF — shared team context written by Planner
        team_brief = _parse_team_brief(context.get("running_md", ""))
        self._log.info(
            "qa_agent.team_brief",
            stack=team_brief.stack,
            test_runner=team_brief.test_runner,
            lint_tool=team_brief.lint_tool,
            coverage_applies=team_brief.coverage_applies,
            coverage_threshold=team_brief.coverage_threshold,
        )

        # 3. Install dependencies based on stack
        await self._install_dependencies(team_brief)

        # 4. Ask Claude for a test plan (now aware of TEAM_BRIEF)
        test_plan = await self._build_test_plan(context)
        self._log.info(
            "qa_agent.test_plan",
            what_to_verify=test_plan.get("what_to_verify", ""),
            test_files=test_plan.get("test_files", []),
            remove_root_conftest=test_plan.get("remove_root_conftest", False),
        )

        # 5. Clean broken conftest if Claude flagged it
        if test_plan.get("remove_root_conftest"):
            self._remove_root_conftest()

        # 6. Apply the test plan using TEAM_BRIEF skills
        self._apply_test_plan(test_plan, context, team_brief)

        # 7. Run tests + lint concurrently (skill-based tools from TEAM_BRIEF)
        test_task = asyncio.create_task(self._run_tests())
        lint_task = asyncio.create_task(self._run_linting(team_brief))
        (junit_path, test_rc), lint_results = await asyncio.gather(test_task, lint_task)

        # 7. Parse results
        suites = _parse_junit_xml(junit_path)
        total_failures = sum(s.failed + s.errored for s in suites)
        total_tests = sum(s.total for s in suites)

        coverage_xml_path = self.repo_path / "coverage.xml"
        coverage = _parse_coverage_xml(coverage_xml_path, self.coverage_threshold)

        # 8. Evaluate outcome
        outcome, blocking_reason = self._evaluate_outcome(
            test_rc=test_rc,
            total_tests=total_tests,
            total_failures=total_failures,
            coverage=coverage,
            lint_results=lint_results,
            team_brief=team_brief,
        )

        quality_evidence = self._build_evidence(
            suites=suites,
            coverage=coverage,
            lint_results=lint_results,
            outcome=outcome,
            test_plan=test_plan,
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

    # ------------------------------------------------------------------
    # Step 2 — gather workspace context
    # ------------------------------------------------------------------

    async def _gather_workspace_context(self) -> dict[str, Any]:
        """
        Collect everything Claude needs to build a test plan:
        - git diff stat and file list vs origin/main
        - RUNNING.md, README.md, ARCHITECTURE.md contents
        - root conftest.py content (to detect broken stubs)
        - full list of existing test files in this branch
        """
        context: dict[str, Any] = {
            "diff_stat": "",
            "changed_files": [],
            "running_md": "",
            "readme_md": "",
            "arch_md": "",
            "conftest_content": "",
            "existing_test_files": [],
        }

        is_git = (self.repo_path / ".git").exists()

        if is_git:
            rc, stat_out, _ = await _run(
                ["git", "diff", "origin/main..HEAD", "--stat"],
                cwd=self.repo_path,
            )
            if rc == 0:
                context["diff_stat"] = stat_out[:3000]

            rc, names_out, _ = await _run(
                ["git", "diff", "origin/main..HEAD", "--name-only"],
                cwd=self.repo_path,
            )
            if rc == 0:
                context["changed_files"] = [f for f in names_out.splitlines() if f]

        # Doc files
        context["running_md"] = self._read_doc("RUNNING.md", 4000)
        context["readme_md"] = self._read_doc("README.md", 2000)
        context["arch_md"] = self._read_doc("ARCHITECTURE.md", 2000)
        context["conftest_content"] = self._read_doc("conftest.py", 1000)

        # All test files currently in the repo (on this branch)
        test_files: list[str] = []
        tests_dir = self.repo_path / "tests"
        if tests_dir.is_dir():
            test_files += [
                str(f.relative_to(self.repo_path))
                for f in sorted(tests_dir.rglob("test_*.py"))
                if f.is_file()
            ]
        test_files += [
            str(f.relative_to(self.repo_path))
            for f in sorted(self.repo_path.glob("test_*.py"))
            if f.is_file()
        ]
        context["existing_test_files"] = test_files

        return context

    def _read_doc(self, filename: str, max_chars: int = 3000) -> str:
        path = self.repo_path / filename
        if not path.exists():
            return ""
        try:
            return path.read_text(errors="replace")[:max_chars]
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Step 3 — LLM test plan
    # ------------------------------------------------------------------

    async def _build_test_plan(self, context: dict) -> dict:
        """
        Ask Claude: given what was built and what test files exist,
        which tests should QA run?  Returns structured plan dict.
        Falls back to {} on any error — caller handles gracefully.
        """
        changed_files_str = "\n".join(context["changed_files"][:60]) or "(no git diff available)"
        test_files_str = "\n".join(context["existing_test_files"][:60]) or "(none found)"

        user_msg = f"""Work order: {self.work_order_title}
Task description: {self.task_description[:800] if self.task_description else "(not provided)"}

Files changed in this branch vs main:
{changed_files_str}

RUNNING.md (how the app works and how to test it):
{context["running_md"] or "(not found)"}

README.md:
{context["readme_md"] or "(not found)"}

Root conftest.py (if any):
{context["conftest_content"] or "(none)"}

All test files currently in this branch's repository:
{test_files_str}

Git diff stat:
{context["diff_stat"] or "(not available)"}

Produce the test plan JSON now."""

        messages = [{"role": "user", "content": user_msg}]

        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._call_openai_sync(_QA_SYSTEM_PROMPT, messages),
            )
            self._log.debug("qa_agent.openai_raw", raw=raw[:300])

            # Extract JSON from response
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                self._log.warning("qa_agent.test_plan.no_json", raw=raw[:200])
                return {}
            plan = json.loads(raw[start:end])

            # Filesystem override: if LLM returned no test_files, glob the repo.
            # LLMs hallucinate "no tests" when they don't recognise the extension
            # (.test.jsx, .spec.tsx, etc.). The filesystem is always authoritative.
            if not plan.get("test_files"):
                globs = [
                    "**/*.test.py", "**/test_*.py",
                    "**/*.test.js", "**/*.test.ts",
                    "**/*.test.jsx", "**/*.test.tsx",
                    "**/*.spec.js", "**/*.spec.ts",
                    "**/*.spec.jsx", "**/*.spec.tsx",
                ]
                found: list[str] = []
                for pattern in globs:
                    found += [
                        str(p.relative_to(self.repo_path))
                        for p in self.repo_path.glob(pattern)
                        if "node_modules" not in str(p) and ".venv" not in str(p)
                    ]
                if found:
                    self._log.info(
                        "qa_agent.test_plan.glob_override",
                        llm_files=0, found=len(found), files=found[:10],
                    )
                    plan["test_files"] = sorted(set(found))

            return plan

        except Exception as exc:
            self._log.warning("qa_agent.test_plan.failed", error=str(exc))
            return {}

    def _call_openai_sync(self, system: str, messages: list[dict]) -> str:
        """Synchronous OpenAI reasoning call (runs in executor from async context)."""
        from phalanx.agents.openai_client import OpenAIClient  # noqa: PLC0415
        from phalanx.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        client = OpenAIClient(model=settings.openai_model_reasoning)
        str_messages = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ]
        return client.call_text(messages=str_messages, system=system, max_tokens=2048)

    def _call_claude(self, system: str, messages: list[dict]) -> str:
        """Synchronous Claude API call (runs in executor from async context)."""
        from phalanx.agents.base import get_anthropic_client  # noqa: PLC0415
        from phalanx.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        client = get_anthropic_client()
        response = client.messages.create(
            model=settings.anthropic_model_fast,
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        self._log.info(
            "qa_agent.claude_call",
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response.content[0].text

    # ------------------------------------------------------------------
    # Step 4 — clean broken conftest
    # ------------------------------------------------------------------

    def _remove_root_conftest(self) -> None:
        """Remove a broken root conftest.py (generated stub that conflicts with pytest-timeout)."""
        conftest = self.repo_path / "conftest.py"
        if not conftest.exists():
            return
        try:
            conftest.unlink()
            self._log.info("qa_agent.conftest.removed")
        except OSError as exc:
            self._log.warning("qa_agent.conftest.remove_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Step 5 — apply test plan
    # ------------------------------------------------------------------

    def _apply_test_plan(self, test_plan: dict, context: dict) -> None:
        """
        Update self.test_command based on Claude's plan.

        Two concerns:
          1. Test files — which tests to run (scoped to this branch's work)
          2. Coverage source — what source to measure (NOT the whole repo)

        Industry standard coverage formula:
          coverage% = lines_executed_in_source / total_lines_in_source
        where source = the module/package the builder wrote, not ".".

        Falls back gracefully at each level if Claude's plan is incomplete.
        """
        # ── Resolve coverage source ────────────────────────────────────────
        # Claude tells us the package/module that was built (e.g. "app", "main").
        # If Claude didn't say, derive from changed non-test source files.
        cov_source = test_plan.get("coverage_source") or self._derive_coverage_source(context)

        # ── Resolve test files ─────────────────────────────────────────────
        llm_files = test_plan.get("test_files", [])
        valid_files = [f for f in llm_files if (self.repo_path / f).exists()]

        if not valid_files:
            # Fallback 1: changed test files on this branch
            changed = context.get("changed_files", [])
            valid_files = [
                f for f in changed
                if (f.startswith("tests/") or "test_" in f)
                and f.endswith(".py")
                and (self.repo_path / f).exists()
            ]
            if valid_files:
                self._log.info("qa_agent.test_scope.from_diff", files=valid_files)
            else:
                # Fallback 2: tests/ dir
                tests_dir = self.repo_path / "tests"
                if tests_dir.is_dir():
                    valid_files = ["tests/"]
                    self._log.info("qa_agent.test_scope.tests_dir_fallback")
                # else: keep DEFAULT_TEST_CMD
        else:
            self._log.info("qa_agent.test_scope.from_llm", files=valid_files)

        # ── Build final pytest command ────────────────────────────────────
        # Extract non-cov flags, then re-add --cov with scoped source.
        base_flags = [
            a for a in self.test_command
            if a.startswith("-") and not a.startswith("--cov")
        ]

        cov_flags = [f"--cov={cov_source}", "--cov-report=xml:coverage.xml"] if cov_source else [
            "--cov=.", "--cov-report=xml:coverage.xml"
        ]

        self._log.info("qa_agent.coverage_scope", source=cov_source or ".")

        if valid_files:
            self.test_command = [self._PYTEST_BIN] + base_flags + cov_flags + valid_files
        else:
            self.test_command = [self._PYTEST_BIN] + base_flags + cov_flags

    def _derive_coverage_source(self, context: dict) -> str | None:
        """
        If Claude didn't specify coverage_source, infer it from the changed
        source files (non-test .py files) in this branch's diff.

        Returns the top-level package/module name (e.g. "app", "main", "api"),
        or None if it can't be determined.

        Logic (in order):
          1. Find changed .py files that are NOT tests and NOT root-level config
          2. If they all share a top-level directory (e.g. app/), return that dir
          3. If they're all root-level .py files (e.g. main.py), return the stem
             of the most likely entry-point file
          4. Return None — caller will fall back to --cov=.
        """
        changed = context.get("changed_files", [])
        source_files = [
            f for f in changed
            if f.endswith(".py")
            and not f.startswith("tests/")
            and "test_" not in f
            and f not in ("conftest.py", "setup.py")
        ]

        if not source_files:
            return None

        # Check if files share a top-level package directory
        top_dirs = {f.split("/")[0] for f in source_files if "/" in f}
        # Filter out common non-source dirs
        top_dirs -= {"tests", "docs", "scripts", "migrations", "alembic"}
        if len(top_dirs) == 1:
            pkg = top_dirs.pop()
            # Verify it exists as a directory
            if (self.repo_path / pkg).is_dir():
                return pkg

        # All root-level .py files — pick main.py or the first one
        root_files = [f for f in source_files if "/" not in f]
        if root_files:
            priority = ["main.py", "app.py", "server.py", "api.py"]
            for pf in priority:
                if pf in root_files:
                    return Path(pf).stem  # "main", "app", etc.
            return Path(root_files[0]).stem

        return None

    # ------------------------------------------------------------------
    # Steps 3–7 — skill-based execution driven by TEAM_BRIEF
    # ------------------------------------------------------------------

    async def _install_dependencies(self, team_brief: TeamBrief) -> None:
        """Install deps based on the stack declared in TEAM_BRIEF."""
        stack = team_brief.stack.lower()
        pip = str(Path(sys.executable).parent / "pip")

        # Python stacks
        if "python" in stack or "fastapi" in stack or "flask" in stack or "django" in stack or not stack:
            for req_file, cmd in [
                ("requirements.txt", [pip, "install", "-r", "requirements.txt", "-q"]),
                ("requirements-dev.txt", [pip, "install", "-r", "requirements-dev.txt", "-q"]),
                ("pyproject.toml", [pip, "install", "-e", ".[dev]", "-q"]),
                ("setup.py", [pip, "install", "-e", ".", "-q"]),
            ]:
                if (self.repo_path / req_file).exists():
                    self._log.info("qa_agent.deps.install", file=req_file)
                    rc, _, stderr = await _run(cmd, cwd=self.repo_path)
                    if rc != 0:
                        self._log.warning("qa_agent.deps.install_failed", file=req_file, stderr=stderr[:300])
                    else:
                        self._log.info("qa_agent.deps.installed", file=req_file)
                    if req_file in ("requirements.txt", "requirements-dev.txt"):
                        break

        # Node/JS stacks — npm install if package.json exists
        if any(k in stack for k in ("node", "react", "typescript", "vite", "next", "express", "javascript")):
            for pkg_json, cwd in [
                ("package.json", self.repo_path),
                ("frontend/package.json", self.repo_path / "frontend"),
            ]:
                if (self.repo_path / pkg_json).exists():
                    self._log.info("qa_agent.deps.install", file=pkg_json)
                    try:
                        rc, _, stderr = await _run(["npm", "install", "--legacy-peer-deps"], cwd=cwd)
                        if rc != 0:
                            self._log.warning("qa_agent.deps.install_failed", file=pkg_json, stderr=stderr[:300])
                        else:
                            self._log.info("qa_agent.deps.installed", file=pkg_json)
                    except FileNotFoundError:
                        self._log.warning("qa_agent.deps.tool_missing", tool="npm",
                                          reason="npm not installed in worker — skipping JS deps")

        # Go stacks
        if "go" in stack or "golang" in stack:
            if (self.repo_path / "go.mod").exists():
                self._log.info("qa_agent.deps.install", file="go.mod")
                try:
                    rc, _, stderr = await _run(["go", "mod", "download"], cwd=self.repo_path)
                    if rc != 0:
                        self._log.warning("qa_agent.deps.install_failed", file="go.mod", stderr=stderr[:300])
                    else:
                        self._log.info("qa_agent.deps.installed", file="go.mod")
                except FileNotFoundError:
                    self._log.warning("qa_agent.deps.tool_missing", tool="go",
                                      reason="go not installed in worker — skipping Go deps")

    def _apply_test_plan(self, test_plan: dict, context: dict, team_brief: TeamBrief) -> None:
        """
        Build self.test_command from TEAM_BRIEF skills + Claude's test plan.

        TEAM_BRIEF defines the runner (pytest, npm test, go test).
        Claude's plan defines which files/packages to target.
        No Python-only heuristics — language-agnostic.
        """
        test_runner = team_brief.test_runner.strip()
        stack = team_brief.stack.lower()

        llm_files = test_plan.get("test_files", [])
        valid_files = [f for f in llm_files if (self.repo_path / f).exists()]

        # ── Python / pytest path ──────────────────────────────────────────────
        if test_runner.startswith("pytest") or (not test_runner and "python" in stack):
            coverage_source = test_plan.get("coverage_source")
            if not coverage_source and team_brief.coverage_applies:
                coverage_source = self._derive_coverage_source(context)

            base_flags = ["--tb=short", "-q", "--junit-xml=test-results.xml"]

            if team_brief.coverage_applies and coverage_source:
                cov_flags = [f"--cov={coverage_source}", "--cov-report=xml:coverage.xml"]
            elif team_brief.coverage_applies:
                cov_flags = ["--cov=.", "--cov-report=xml:coverage.xml"]
            else:
                cov_flags = []  # No coverage for this stack

            self._log.info("qa_agent.coverage_scope",
                           source=coverage_source or ("." if team_brief.coverage_applies else "N/A"))

            if not valid_files:
                valid_files = self._fallback_test_files(context)

            if valid_files:
                self.test_command = [self._PYTEST_BIN] + base_flags + cov_flags + valid_files
            else:
                self.test_command = [self._PYTEST_BIN] + base_flags + cov_flags

            if valid_files:
                self._log.info("qa_agent.test_scope.from_llm" if test_plan.get("test_files") else
                               "qa_agent.test_scope.fallback", files=valid_files)

        # ── npm test (React/Node/TypeScript) ─────────────────────────────────
        elif "npm" in test_runner or "jest" in test_runner or "vitest" in test_runner:
            # npm test produces its own output; no JUnit by default
            # We run it and check exit code — coverage handled by the test framework
            parts = test_runner.split()
            self.test_command = parts
            self._log.info("qa_agent.coverage_scope", source="N/A (npm test handles coverage)")

        # ── go test ───────────────────────────────────────────────────────────
        elif "go test" in test_runner:
            parts = test_runner.split()
            if team_brief.coverage_applies:
                if "-cover" not in parts:
                    parts.append("-cover")
            self.test_command = parts
            self._log.info("qa_agent.coverage_scope", source="go test -cover")

        # ── fallback: split whatever test_runner says ─────────────────────────
        else:
            self.test_command = test_runner.split() if test_runner else self.test_command
            self._log.info("qa_agent.coverage_scope", source="custom runner")

    def _fallback_test_files(self, context: dict) -> list[str]:
        """Find test files from diff or tests/ dir when LLM didn't specify any."""
        changed = context.get("changed_files", [])
        from_diff = [
            f for f in changed
            if (f.startswith("tests/") or "test_" in f)
            and f.endswith(".py")
            and (self.repo_path / f).exists()
        ]
        if from_diff:
            self._log.info("qa_agent.test_scope.from_diff", files=from_diff)
            return from_diff
        tests_dir = self.repo_path / "tests"
        if tests_dir.is_dir():
            self._log.info("qa_agent.test_scope.tests_dir_fallback")
            return ["tests/"]
        return []

    async def _run_tests(self) -> tuple[Path, int]:
        junit_path = self.repo_path / "test-results.xml"
        self._log.info("qa_agent.tests.start", cmd=self.test_command)
        try:
            rc, stdout, stderr = await _run(self.test_command, cwd=self.repo_path)
        except FileNotFoundError:
            tool = self.test_command[0] if self.test_command else "unknown"
            self._log.warning(
                "qa_agent.tests.tool_missing",
                tool=tool,
                reason="binary not installed in worker — test run skipped",
            )
            # Write a failure record so the evaluator knows tests couldn't run
            junit_path.write_text(
                '<?xml version="1.0"?>'
                '<testsuite name="qa" tests="1" failures="1" errors="0" skipped="0" time="0">'
                f'<testcase classname="qa" name="tool_missing">'
                f'<failure message="tool not installed">{tool} binary not found in worker container</failure>'
                '</testcase>'
                '</testsuite>'
            )
            return junit_path, 1
        self._log.info("qa_agent.tests.done", rc=rc)

        # ── npm peer-dep auto-repair ──────────────────────────────────────────
        # If vitest/jest fails with "Cannot find module '<pkg>'", install the
        # missing package and retry once. This handles peer deps that the builder
        # forgot to list in package.json (e.g. @testing-library/dom).
        if rc != 0 and self.test_command and self.test_command[0] in ("npx", "npm"):
            combined = stdout + stderr
            import re as _re
            missing_match = _re.search(
                r"Cannot find module '([@\w][^']*)'", combined
            )
            if missing_match:
                missing_pkg = missing_match.group(1).split("/")
                # Reconstruct scoped package name (e.g. @testing-library/dom)
                if missing_pkg[0].startswith("@") and len(missing_pkg) >= 2:
                    pkg_to_install = "/".join(missing_pkg[:2])
                else:
                    pkg_to_install = missing_pkg[0]
                self._log.info(
                    "qa_agent.npm.peer_dep_repair",
                    missing=pkg_to_install,
                )
                repair_rc, _, repair_err = await _run(
                    ["npm", "install", "--save-dev", pkg_to_install],
                    cwd=self.repo_path,
                )
                if repair_rc == 0:
                    self._log.info("qa_agent.npm.peer_dep_installed", pkg=pkg_to_install)
                    # Retry test run once
                    if junit_path.exists():
                        junit_path.unlink()
                    rc, stdout, stderr = await _run(self.test_command, cwd=self.repo_path)
                    self._log.info("qa_agent.tests.retry_done", rc=rc)
                else:
                    self._log.warning(
                        "qa_agent.npm.peer_dep_repair_failed",
                        pkg=pkg_to_install, stderr=repair_err[:200],
                    )

        # For non-pytest runners (npm test, go test), synthesize a minimal JUnit XML
        # so _parse_junit_xml can return a result count.
        if not junit_path.exists() and rc == 0:
            junit_path.write_text(
                '<?xml version="1.0"?>'
                '<testsuite name="qa" tests="1" failures="0" errors="0" skipped="0" time="0">'
                '<testcase classname="qa" name="tests_passed"/>'
                '</testsuite>'
            )
        elif not junit_path.exists() and rc != 0:
            # Write failure record so evaluator picks up the failure
            escaped = (stdout + stderr)[:500].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            junit_path.write_text(
                '<?xml version="1.0"?>'
                f'<testsuite name="qa" tests="1" failures="1" errors="0" skipped="0" time="0">'
                f'<testcase classname="qa" name="tests_failed">'
                f'<failure message="test runner failed">{escaped}</failure>'
                f'</testcase>'
                f'</testsuite>'
            )
        return junit_path, rc

    async def _run_linting(self, team_brief: TeamBrief) -> list[LintResult]:
        """Run lint tool(s) specified in TEAM_BRIEF. Language-agnostic."""
        results: list[LintResult] = []
        lint_tool = team_brief.lint_tool.strip().lower()

        if lint_tool in ("none", "", "n/a"):
            self._log.info("qa_agent.lint.skipped", reason="lint_tool=none in TEAM_BRIEF")
            return results

        # ── ruff (Python) ─────────────────────────────────────────────────────
        if "ruff" in lint_tool:
            rc, stdout, _ = await _run(["ruff", "check", "."], cwd=self.repo_path)
            violation_count = len([l for l in stdout.splitlines() if l.strip() and not l.startswith("Found")])
            results.append(LintResult(tool="ruff-check", passed=rc == 0,
                                      violation_count=violation_count, output=stdout[:3000]))

            rc, stdout, _ = await _run(["ruff", "format", "--check", "."], cwd=self.repo_path)
            results.append(LintResult(tool="ruff-format", passed=rc == 0,
                                      violation_count=0 if rc == 0 else 1, output=stdout[:1000]))

        # ── eslint (JS/TS) ────────────────────────────────────────────────────
        elif "eslint" in lint_tool:
            cmd = lint_tool.split()
            try:
                rc, stdout, stderr = await _run(cmd, cwd=self.repo_path)
                violation_count = stdout.count("error") + stdout.count("warning")
                results.append(LintResult(tool="eslint", passed=rc == 0,
                                          violation_count=violation_count, output=stdout[:3000]))
            except FileNotFoundError:
                self._log.warning("qa_agent.lint.tool_missing", tool="eslint")

        # ── golangci-lint / go vet ────────────────────────────────────────────
        elif "golangci" in lint_tool or "go vet" in lint_tool:
            cmd = lint_tool.split()
            try:
                rc, stdout, stderr = await _run(cmd, cwd=self.repo_path)
                results.append(LintResult(tool=cmd[0], passed=rc == 0,
                                          violation_count=0 if rc == 0 else 1, output=(stdout + stderr)[:3000]))
            except FileNotFoundError:
                self._log.warning("qa_agent.lint.tool_missing", tool=cmd[0])

        # ── generic: run whatever command TEAM_BRIEF specified ────────────────
        else:
            cmd = lint_tool.split()
            try:
                rc, stdout, stderr = await _run(cmd, cwd=self.repo_path)
                results.append(LintResult(tool=cmd[0] if cmd else "lint", passed=rc == 0,
                                          violation_count=0 if rc == 0 else 1, output=(stdout + stderr)[:3000]))
            except FileNotFoundError:
                self._log.warning("qa_agent.lint.tool_missing", tool=cmd[0] if cmd else "lint")

        return results

    def _evaluate_outcome(
        self,
        test_rc: int,
        total_tests: int,
        total_failures: int,
        coverage: CoverageResult | None,
        lint_results: list[LintResult],
        team_brief: TeamBrief | None = None,
    ) -> tuple[QAOutcome, str | None]:
        reasons: list[str] = []

        if total_tests == 0:
            reasons.append("No tests found — builder must add tests for new code.")
        elif total_failures > 0:
            reasons.append(f"{total_failures} test(s) failed.")

        # Only enforce coverage if TEAM_BRIEF says it applies
        coverage_applies = team_brief.coverage_applies if team_brief else True
        if coverage_applies and coverage and not coverage.threshold_met:
            reasons.append(
                f"Coverage {coverage.line_coverage_pct}% is below threshold "
                f"{coverage.threshold}%."
            )

        # Lint is advisory — reported in evidence but does NOT block QA pass.
        # Lint gate belongs in the Reviewer agent, not here. Generated code
        # often has minor style issues that don't affect correctness.

        if reasons:
            return QAOutcome.FAILED, " | ".join(reasons)
        return QAOutcome.PASSED, None

    def _build_evidence(
        self,
        suites: list[TestSuiteResult],
        coverage: CoverageResult | None,
        lint_results: list[LintResult],
        outcome: QAOutcome,
        test_plan: dict | None = None,
    ) -> dict[str, Any]:
        total = sum(s.total for s in suites)
        passed = sum(s.passed for s in suites)
        failed = sum(s.failed + s.errored for s in suites)

        evidence: dict[str, Any] = {
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
            "failures": [f for suite in suites for f in suite.failures],
            "modules_below_coverage": coverage.modules_below_threshold if coverage else [],
        }
        if test_plan:
            evidence["test_plan"] = {
                "what_to_verify": test_plan.get("what_to_verify", ""),
                "rationale": test_plan.get("rationale", ""),
                "test_files": test_plan.get("test_files", []),
            }
        return evidence

    async def _persist_artifact(self, report: QAReport) -> None:
        try:
            import hashlib  # noqa: PLC0415

            from sqlalchemy import select  # noqa: PLC0415

            from phalanx.db.models import Artifact, Run  # noqa: PLC0415
            from phalanx.db.session import get_db  # noqa: PLC0415

            json_bytes = json.dumps(report.as_dict()).encode()
            content_hash = hashlib.sha256(json_bytes).hexdigest()

            async with get_db() as session:
                row = await session.execute(select(Run.project_id).where(Run.id == str(self.run_id)))
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
        """
        On QA pass: do nothing — the task is marked COMPLETED by the Celery
        entry point, and advance_run will pick up the next pending task
        (security, reviewer, release, SRE) automatically.

        On QA fail: set Run to FAILED so the pipeline halts immediately.
        """
        if report.outcome == QAOutcome.PASSED:
            self._log.info("qa_agent.run_status_updated", new_status="EXECUTING (unchanged)")
            return

        try:
            from sqlalchemy import update  # noqa: PLC0415

            from phalanx.db.models import Run  # noqa: PLC0415
            from phalanx.db.session import get_db  # noqa: PLC0415

            async with get_db() as session:
                await session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(status="FAILED", updated_at=datetime.now(UTC))
                )
                await session.commit()

            self._log.info("qa_agent.run_status_updated", new_status="FAILED")
        except Exception as exc:
            self._log.warning("qa_agent.status_update_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


from phalanx.queue.celery_app import celery_app as _celery  # noqa: E402


@_celery.task(
    name="phalanx.agents.qa.execute_task",
    bind=True,
    queue="qa",
    max_retries=2,
    acks_late=True,
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """
    Celery entry point: run the QA pipeline for a single task.
    """
    import asyncio  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy import select, update  # noqa: PLC0415

    from phalanx.config.settings import get_settings  # noqa: PLC0415
    from phalanx.db.models import Run, Task, WorkOrder  # noqa: PLC0415
    from phalanx.db.session import get_db  # noqa: PLC0415

    _settings = get_settings()

    async def _run_async():
        async with get_db() as session:
            task_result = await session.execute(select(Task).where(Task.id == task_id))
            task = task_result.scalar_one_or_none()
            if task is None:
                return {"success": False, "error": f"Task {task_id} not found"}

            run_result = await session.execute(select(Run).where(Run.id == run_id))
            run = run_result.scalar_one()

            wo_result = await session.execute(
                select(WorkOrder).where(WorkOrder.id == run.work_order_id)
            )
            work_order = wo_result.scalar_one_or_none()
            work_order_title = work_order.title if work_order else ""

        # Resolve workspace from last builder task output
        builder_workspace: str | None = None
        async with get_db() as session:
            from sqlalchemy import and_  # noqa: PLC0415

            builder_result = await session.execute(
                select(Task)
                .where(
                    and_(
                        Task.run_id == run_id,
                        Task.agent_role.in_(["builder", "component_builder", "page_assembler"]),
                        Task.status == "COMPLETED",
                    )
                )
                .order_by(Task.sequence_num.desc())
                .limit(1)
            )
            last_builder = builder_result.scalar_one_or_none()
            if last_builder and isinstance(last_builder.output, dict):
                builder_workspace = last_builder.output.get("workspace")

        if builder_workspace:
            workspace = Path(builder_workspace)
        else:
            workspace = Path(_settings.git_workspace) / run.project_id / run_id

        agent = QAAgent(
            run_id=run.id,
            task_id=task.id,
            repo_path=workspace,
            task_description=task.description,
            work_order_title=work_order_title,
        )

        try:
            report = await agent.evaluate()
            outcome = report.outcome

            task_status = "COMPLETED" if outcome == QAOutcome.PASSED else "FAILED"
            async with get_db() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status=task_status,
                        output=report.as_dict(),
                        error=(report.blocking_reason if outcome != QAOutcome.PASSED else None),
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

    result = asyncio.run(_run_async())
    return result
