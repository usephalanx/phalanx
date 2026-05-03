"""Fixture 07 — coverage threshold (TL must ADD a test file, not modify code).

Source pattern: any Python repo using pytest-cov with --cov-fail-under.
A new feature module gets merged without tests; CI enforces coverage
gate; PR blocks. Real shape pulled from many maintainer PRs that follow
this exact arc.

This fixture is unique in the corpus: the fix is to CREATE a NEW file
(a test file), not modify an existing one. Validates that TL's
task_plan can express file creation via apply_diff (with a "new file
mode" diff) or insert (writing fresh content to a not-yet-existing path).

What v1.7 TL must produce:
  - task_plan: [engineer (creates tests/test_calc.py + commit + push), sre_verify]
  - SRE setup may or may not be needed depending on whether pytest-cov
    is in dev deps; in this fixture it IS, so no setup task expected
  - root_cause mentions "coverage" + the under-tested module
  - confidence: ≥ 0.6 (writing tests is mostly mechanical but TL has to
    pick the right test cases; not as cut-and-dried as a one-line replace)

Why this fixture matters:
  - First "create new file" shape in the corpus.
  - Validates apply_diff can express new-file creation via
    "--- /dev/null" + "+++ b/<path>" header.
  - Stress-tests TL's understanding that coverage failures aren't
    "bugs in code" — they're missing tests.
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_excludes_agent,
    plan_includes_agent,
    root_cause_mentions,
)


def _engineer_adds_tests_for_statistics_helpers():
    """Accept either: a NEW test file targeting the statistics module,
    OR appending tests to an existing test file. Both are valid fixes
    for a coverage gap. Catches: TL touching src/ instead of tests/.
    """
    def _check(output: dict) -> None:
        plan = output.get("task_plan") or []
        keywords = {"mean", "variance", "stddev"}
        for ts in plan:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                action = step.get("action")
                file_path = step.get("file") or ""
                # Direct file-level edits to a tests/* file containing
                # one of the statistics keywords:
                if file_path.startswith("tests/") and action in {
                    "insert", "replace"
                }:
                    blob = (step.get("new") or "") + (step.get("content") or "")
                    if any(k in blob for k in keywords):
                        return
                if action == "apply_diff":
                    diff = step.get("diff") or ""
                    if "tests/" in diff and any(k in diff for k in keywords):
                        return
        raise AssertionError(
            "no engineer step adds tests for statistics helpers "
            "(mean/variance/stddev) in any tests/ file"
        )
    _check.__name__ = "engineer_adds_tests_for_statistics_helpers"
    return _check

CI_LOG = """\
2026-04-29T18:42:11.001Z + python -m pytest tests/ --cov=src/calc --cov-fail-under=80 --timeout=2
2026-04-29T18:42:13.221Z =================== test session starts ===================
2026-04-29T18:42:13.221Z platform linux -- Python 3.11.9, pytest-8.2.2, pytest-cov-5.0
2026-04-29T18:42:13.221Z collected 4 items
2026-04-29T18:42:13.345Z
2026-04-29T18:42:13.345Z tests/test_calc_basic.py ....                                [100%]
2026-04-29T18:42:13.345Z
2026-04-29T18:42:13.345Z ---------- coverage: platform linux, python 3.11.9 ----------
2026-04-29T18:42:13.345Z Name                       Stmts   Miss  Cover   Missing
2026-04-29T18:42:13.345Z --------------------------------------------------------------
2026-04-29T18:42:13.345Z src/calc/__init__.py            2      0   100%
2026-04-29T18:42:13.345Z src/calc/basic.py              12      0   100%
2026-04-29T18:42:13.345Z src/calc/statistics.py         18     14    22%   8-14, 18-26
2026-04-29T18:42:13.345Z --------------------------------------------------------------
2026-04-29T18:42:13.345Z TOTAL                          32     14    56%
2026-04-29T18:42:13.345Z FAIL Required test coverage of 80% not reached. Total coverage: 56.25%
2026-04-29T18:42:13.350Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "src/calc/__init__.py": "from .basic import add, sub  # noqa: F401\n",
    "src/calc/basic.py": (
        "def add(a: float, b: float) -> float:\n"
        "    return a + b\n\n\n"
        "def sub(a: float, b: float) -> float:\n"
        "    return a - b\n"
    ),
    "src/calc/statistics.py": (
        '"""Stats helpers — added in this PR; no tests yet."""\n\n\n'
        "def mean(values: list[float]) -> float:\n"
        "    if not values:\n"
        "        raise ValueError(\"mean of empty\")\n"
        "    return sum(values) / len(values)\n\n\n"
        "def variance(values: list[float]) -> float:\n"
        "    if len(values) < 2:\n"
        "        raise ValueError(\"variance needs ≥2\")\n"
        "    mu = mean(values)\n"
        "    return sum((x - mu) ** 2 for x in values) / (len(values) - 1)\n\n\n"
        "def stddev(values: list[float]) -> float:\n"
        "    return variance(values) ** 0.5\n"
    ),
    "tests/test_calc_basic.py": (
        "from calc import add, sub\n\n\n"
        "def test_add(): assert add(2, 3) == 5\n"
        "def test_sub(): assert sub(5, 2) == 3\n"
        "def test_add_zero(): assert add(0, 0) == 0\n"
        "def test_sub_negative(): assert sub(-1, -2) == 1\n"
    ),
    # tests/test_calc_statistics.py is INTENTIONALLY MISSING — that's the bug.
    "pyproject.toml": (
        "[project]\n"
        "name = \"calc\"\n"
        "version = \"0.3.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\", \"pytest-cov>=5\"]\n"
    ),
}


FIXTURE = CorpusFixture(
    name="07_coverage_threshold_add_test",
    description=(
        "pytest-cov fails because src/calc/statistics.py was added without "
        "tests (22% coverage on the file vs 80% global threshold). Fix: "
        "ADD tests/test_calc_statistics.py covering mean/variance/stddev. "
        "TL must express NEW-file creation in task_plan, not just modify "
        "an existing one."
    ),
    source_repo="(synthesized; common pytest-cov gate failure shape)",
    source_pr_or_commit="N/A — coverage threshold class",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/ --cov=src/calc --cov-fail-under=80",
    failing_job_name="test",
    pr_number=44,
    invariants=[
        root_cause_mentions("coverage", "statistics"),
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # The KEY invariant: tests must be ADDED for the statistics
        # helpers — accepts either a NEW test file or extending an
        # existing one (both are valid maintainer-style fixes).
        _engineer_adds_tests_for_statistics_helpers(),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        # Lower bar — TL has to choose test cases, not just apply a recipe
        confidence_at_least(0.6),
    ],
)
