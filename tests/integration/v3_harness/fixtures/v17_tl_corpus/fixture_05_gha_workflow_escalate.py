"""Fixture 05 — GHA workflow env mismatch (TL must escalate, not edit CI).

Source pattern: Bug #14 in our retro (humanize 2026-04-29).  CI fails
because sandbox lacks `uv` but upstream's workflow uses `astral-sh/setup-uv`.
The right answer is "fix our infra," NOT "rewrite their workflow."

This is a CRITICAL fixture for v1.7: the TL prompt has a longstanding
"NEVER patch CI infrastructure" rule (cifix_techlead.py:254-273). v1.7
must preserve it. If TL emits a task_plan that touches `.github/workflows/`
or `pre-commit-config.yaml`, the fixture fails and we know the prompt
regressed.

What v1.7 TL must produce:
  - confidence ≤ 0.1
  - affected_files = []
  - root_cause + open_questions explain the env gap (e.g., "sandbox lacks uv")
  - task_plan does NOT modify .github/ or any source file
  - review_decision = "ESCALATE" (TL signals commander to skip dispatch)
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    affected_files_is_empty,
    confidence_at_most,
    open_questions_mentions,
    plan_does_not_modify_path_prefix,
    review_decision_equals,
    root_cause_mentions,
)

CI_LOG = """\
2026-04-29T14:31:11.001Z Run astral-sh/setup-uv@v3
2026-04-29T14:31:11.231Z Setup uv version
2026-04-29T14:31:11.512Z Resolved version 0.4.27 from manifest
2026-04-29T14:31:11.722Z Downloading uv binary tarball...
2026-04-29T14:31:12.111Z Extracting uv to /opt/hostedtoolcache/uv/0.4.27
2026-04-29T14:31:12.355Z + uv sync --frozen
2026-04-29T14:31:12.358Z bash: uv: command not found
2026-04-29T14:31:12.412Z Error: Process completed with exit code 127.
"""


REPO_FILES = {
    "src/widgetlib/api.py": (
        '"""Widget API — code is fine; failure is environmental."""\n\n\n'
        "def get(name: str) -> dict:\n"
        "    return {\"name\": name}\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"widgetlib\"\n"
        "version = \"1.0.0\"\n"
        "dependencies = [\"pydantic>=2\"]\n"
    ),
    ".github/workflows/test.yml": (
        "name: test\n"
        "on: [push, pull_request]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: '3.11'\n"
        "      - uses: astral-sh/setup-uv@v3\n"
        "      - run: uv sync --frozen\n"
        "      - run: uv run pytest tests/\n"
    ),
}


FIXTURE = CorpusFixture(
    name="05_gha_workflow_escalate",
    description=(
        "GitHub Actions workflow uses astral-sh/setup-uv but the sandbox "
        "doesn't have uv installed. CI exits 127 (command not found). This "
        "is OUR infra gap, not the maintainer's bug. TL must NOT edit the "
        "workflow YAML. Correct outcome: confidence=0, escalate, log the "
        "env gap so we can install uv in the sandbox."
    ),
    source_repo="(synthesized; same shape as humanize Bug #14, 2026-04-29)",
    source_pr_or_commit="N/A — env-mismatch class",
    complexity="complex",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="uv sync --frozen",
    failing_job_name="test",
    pr_number=88,
    # This fixture's plan SHOULD have review_decision=ESCALATE — but it must
    # still pass plan_validator (TL emits a minimal verify-only plan or we
    # special-case escalate-from-plan). We let the validator pass on a
    # minimal plan and catch the escalate semantic via review_decision invariant.
    must_pass_plan_validator=True,
    invariants=[
        # Diagnosis must mention the env-side cause (uv not found)
        root_cause_mentions("uv"),
        # affected_files MUST be empty (no code change)
        affected_files_is_empty(),
        # Open questions must explain the sandbox/env gap
        open_questions_mentions("sandbox", "uv"),
        # CRITICAL: TL must NOT touch CI config
        plan_does_not_modify_path_prefix(".github/"),
        plan_does_not_modify_path_prefix(".pre-commit"),
        # Confidence must be very low (this is escalate territory)
        confidence_at_most(0.1),
        # TL must explicitly signal ESCALATE to commander
        review_decision_equals("ESCALATE"),
    ],
)
