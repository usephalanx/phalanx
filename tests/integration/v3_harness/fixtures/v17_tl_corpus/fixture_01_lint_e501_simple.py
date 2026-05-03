"""Fixture 01 — Ruff E501 on a single Python line (simplest shape).

Source pattern: any Python repo using Ruff with line-length=100. Real
example shape pulled from public Ruff rule docs. This is the SIMPLEST
fix shape — single-line shorten, no SRE setup needed (the lint runner
is already in repo's dev deps), one engineer task, one verify.

Why this fixture matters:
  - Validates TL can produce minimal task_plan (no SRE setup).
  - Validates env_requirements is empty/absent for trivial fixes.
  - Confirms TL doesn't over-engineer (no anticipated cascades for a
    line-length fix).

What TL should produce:
  - task_plan: [engineer (replace step + commit + push), sre_verify]
  - env_requirements: minimal (just `ruff` in python_packages, since
    sre_verify still needs to run ruff)
  - confidence: ≥ 0.8 (clear-cut bug)
  - root_cause mentions "E501" and "line"
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_excludes_agent,
    plan_includes_agent,
    plan_steps_modify,
    root_cause_mentions,
    step_count_in_engineer_task_at_least,
)

CI_LOG = """\
2026-04-29T10:14:22.345Z ╭───────── ruff check ─────────╮
2026-04-29T10:14:22.346Z │ ruff 0.4.10                  │
2026-04-29T10:14:22.346Z ╰──────────────────────────────╯
2026-04-29T10:14:22.412Z src/widgetlib/builders.py:42:101: E501 Line too long (118 > 100)
2026-04-29T10:14:22.412Z    |
2026-04-29T10:14:22.412Z 41 | def build_widget_with_overlong_signature(
2026-04-29T10:14:22.412Z 42 |     name: str, label: str, color: str, size: int, padding: int = 0, margin: int = 0, debug: bool = False) -> Widget:
2026-04-29T10:14:22.412Z    |                                                                                                     ^^^^^^^^^^^^^^^^^^^ E501
2026-04-29T10:14:22.412Z    |
2026-04-29T10:14:22.412Z Found 1 error.
2026-04-29T10:14:22.420Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "src/widgetlib/builders.py": (
        '"""Widget builder functions."""\n\n\n'
        "class Widget:\n"
        "    def __init__(self, name: str) -> None:\n"
        "        self.name = name\n\n\n"
        # Line 42 — too long by design (matches CI log byte-for-byte)
        "def build_widget_with_overlong_signature(\n"
        "    name: str, label: str, color: str, size: int, padding: int = 0, "
        "margin: int = 0, debug: bool = False) -> Widget:\n"
        "    return Widget(name)\n"
    ),
    "pyproject.toml": (
        "[tool.ruff]\n"
        "line-length = 100\n"
        "\n"
        "[project]\n"
        "name = \"widgetlib\"\n"
        "version = \"0.1.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"ruff==0.4.10\", \"pytest>=7\"]\n"
    ),
}


FIXTURE = CorpusFixture(
    name="01_lint_e501_simple",
    description=(
        "Single-line Ruff E501 — line too long. Trivial fix: break the "
        "function signature across lines. No SRE provisioning beyond "
        "ruff itself (which is in pyproject's dev deps)."
    ),
    source_repo="(synthesized; pattern from many Python repos)",
    source_pr_or_commit="N/A — generic Ruff E501",
    complexity="simple",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="ruff check src/widgetlib/builders.py",
    failing_job_name="lint",
    pr_number=42,
    invariants=[
        # Diagnosis must mention the actual error code from the log
        root_cause_mentions("E501"),
        # Plan must include engineer + verify
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # SIMPLE shape: NO SRE setup task — ruff is already a dev dep
        plan_excludes_agent("cifix_sre_setup"),
        # Engineer task must modify the actual file from the log
        plan_steps_modify("src/widgetlib/builders.py"),
        # Engineer task must include commit + push (full cycle, not just edit)
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        # Engineer needs ≥ 3 steps for a real fix (modify + commit + push)
        step_count_in_engineer_task_at_least(3),
        # Confidence should be high — this is unambiguous
        confidence_at_least(0.8),
    ],
)
