"""Fixture 04 — pytest exit 4 trap (the Bug #16 shape).

Source: real shape from many "delete a broken test" fixes. Notable
example — pytest-dev/pytest issue #2393 documented the exit-4 semantics
that bit us on humanize 2026-04-30 (Bug #16, fixed in v1.5.0 with the
verify_command/verify_success contract).

The bug-in-the-bug: the failing test was tests/test_legacy.py::test_old_api,
which the maintainer's PR DELETES (replacing with newer test). After
the test is deleted, running `pytest tests/test_legacy.py::test_old_api`
exits 4 ("no tests collected"), which v1.4.x mistook for failure.

What v1.7 TL must produce (testing v1.5.0 verify-contract carry-forward):
  - failing_command targets the deleted test (e.g., pytest path::test)
  - verify_command targets the WHOLE module/suite (broader)
  - verify_success.exit_codes = [0]
  - engineer's step is "delete_lines" or apply_diff that removes the test
  - plan terminates in sre_verify with the broadened command

Why this fixture matters:
  - Confirms TL still respects v1.5.0 verify-broadening rule for delete fixes.
  - Validates that v1.7's apply_diff/delete_lines actions handle test removal.
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_includes_agent,
    plan_steps_modify,
    root_cause_mentions,
    verify_command_targets_broader_than_failing,
)

CI_LOG = """\
2026-04-30T16:55:01.121Z + python -m pytest tests/test_legacy.py::test_old_api -xvs
2026-04-30T16:55:02.541Z =================== test session starts ===================
2026-04-30T16:55:02.541Z platform linux -- Python 3.11.9, pytest-8.2.2
2026-04-30T16:55:02.541Z collected 1 item
2026-04-30T16:55:02.555Z
2026-04-30T16:55:02.555Z tests/test_legacy.py::test_old_api FAILED
2026-04-30T16:55:02.555Z
2026-04-30T16:55:02.555Z ============= FAILURES =============
2026-04-30T16:55:02.555Z _________ test_old_api _________
2026-04-30T16:55:02.555Z
2026-04-30T16:55:02.555Z     def test_old_api():
2026-04-30T16:55:02.555Z         from samplelib.legacy import old_get
2026-04-30T16:55:02.555Z >       result = old_get("/widgets")
2026-04-30T16:55:02.555Z E       AttributeError: module 'samplelib.legacy' has no attribute 'old_get'
2026-04-30T16:55:02.555Z
2026-04-30T16:55:02.555Z tests/test_legacy.py:18: AttributeError
2026-04-30T16:55:02.555Z =========== short test summary info ===========
2026-04-30T16:55:02.555Z FAILED tests/test_legacy.py::test_old_api
2026-04-30T16:55:02.555Z =========== 1 failed in 0.41s ===========
2026-04-30T16:55:02.560Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "src/samplelib/legacy.py": (
        '"""Legacy API — old_get was removed in PR #200; we should remove the test."""\n'
        "# old_get was removed; nothing here.\n"
    ),
    "tests/test_legacy.py": (
        "from samplelib.legacy import old_get  # broken import — old_get is gone\n\n\n"
        "def test_old_api():\n"
        "    result = old_get(\"/widgets\")\n"
        "    assert result == \"ok\"\n"
    ),
    "tests/test_modern.py": (
        "from samplelib.modern import get\n\n\n"
        "def test_modern_api():\n"
        "    assert get(\"/widgets\") == \"ok\"\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"samplelib\"\n"
        "version = \"2.0.0\"\n"
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\"]\n"
    ),
}


FIXTURE = CorpusFixture(
    name="04_pytest_delete_test_exit_4",
    description=(
        "Maintainer removed `old_get` in PR #200 but kept tests/test_legacy.py "
        "around. The test now imports a missing name. Correct fix: delete the "
        "obsolete test file. Trap: running pytest on the deleted test exits 4 "
        "('no tests collected') which is NOT failure. TL must broaden "
        "verify_command to the parent tests/ directory."
    ),
    source_repo="(synthesized; pytest exit-4 documented in pytest-dev/pytest #2393)",
    source_pr_or_commit="N/A — generic stale-test cleanup",
    complexity="medium",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/test_legacy.py::test_old_api -xvs",
    failing_job_name="test",
    pr_number=201,
    invariants=[
        # Diagnosis must mention the actual broken import / removed function
        root_cause_mentions("old_get"),
        # Plan: engineer (delete) + verify (broadened)
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # Engineer must touch the failing test file (whether by delete_lines,
        # replace, or apply_diff with full removal)
        plan_steps_modify("tests/test_legacy.py"),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        # The CRITICAL invariant: verify_command MUST broaden beyond the
        # failing_command (which is the deleted test selector) — otherwise
        # exit 4 trap returns. v1.5.0 contract carry-forward.
        verify_command_targets_broader_than_failing(),
        confidence_at_least(0.7),
    ],
)
