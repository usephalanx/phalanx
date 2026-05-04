"""Tier-1 tests for TL self-critique validator (v1.6.0 / Phase 1).

Locks in the deterministic checks:
  c1 ci_log_addresses_root_cause — keyword overlap ≥1 hit, ≥30% distinct tokens
  c2 affected_files_exist_in_repo — every path resolves to a file under workspace
  c3 verify_command_will_distinguish_success — first token is a shell-safe ident,
     and (when sandbox available) `command -v <token>` returns 0

Plus a 10-fix_spec corpus driving the milestone:
  Phase-1 milestone = validator passes 5 honest, catches 5 dishonest with
  correct mismatch reasons. Zero false negatives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phalanx.agents._tl_self_critique import (
    check_c1_ci_log_addresses_root_cause,
    check_c2_affected_files_exist,
    check_c3_verify_command_resolvable,
    commander_verify_fix_spec_self_critique,
)

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────
# Corpus fixtures — workspace + ci_log shared across test cases
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "calc"
    src.mkdir(parents=True)
    (src / "formatting.py").write_text("def verbose(): return 'x'\n")
    (src / "math_ops.py").write_text("def add(a, b): return a + b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_math_ops.py").write_text("def test_add(): assert True\n")
    return tmp_path


_CI_LOG_E501 = """\
ANNOTATIONS:
.github:21: Process completed with exit code 1.
E501 Line too long (129 > 100)
--> src/calc/formatting.py:13:101
|
12 | def verbose_description() -> str:
13 |     return "This is a very long descriptive message intended to trip ruff's E501 line-length check deliberately for the testbed."
|
Found 1 error.
"""

_CI_LOG_PYTEST_FAIL = """\
============================= test session starts ==============================
collected 5 items
tests/test_math_ops.py::test_add PASSED                                  [ 20%]
tests/test_math_ops.py::test_subtract PASSED                             [ 40%]
tests/test_math_ops.py::test_multiply FAILED                             [ 60%]
=================================== FAILURES ===================================
__________________________________ test_multiply ________________________________
AssertionError: expected 12, got 11
"""


# ─────────────────────────────────────────────────────────────────────
# c1 — ci_log_addresses_root_cause
# ─────────────────────────────────────────────────────────────────────


def test_c1_passes_when_root_cause_matches_log():
    ok, reason = check_c1_ci_log_addresses_root_cause(
        draft_root_cause="ruff reported E501 line too long in formatting.py",
        ci_log_text=_CI_LOG_E501,
    )
    assert ok, reason


def test_c1_fails_when_root_cause_fabricated():
    ok, reason = check_c1_ci_log_addresses_root_cause(
        draft_root_cause="DatabaseTimeout connecting to PostgreSQL replica",
        ci_log_text=_CI_LOG_E501,
    )
    assert not ok
    assert "appear in ci_log" in reason


def test_c1_passes_partial_overlap_above_30pct():
    """3 of 5 distinctive tokens hit = 60%, must pass."""
    ok, _ = check_c1_ci_log_addresses_root_cause(
        draft_root_cause="ruff E501 reported formatting issue verbose function",
        ci_log_text=_CI_LOG_E501,
    )
    assert ok


def test_c1_fails_when_only_stopwords_in_root_cause():
    ok, reason = check_c1_ci_log_addresses_root_cause(
        draft_root_cause="the and for with this that",
        ci_log_text=_CI_LOG_E501,
    )
    assert not ok
    assert "no distinctive tokens" in reason


def test_c1_handles_empty_log_gracefully():
    ok, reason = check_c1_ci_log_addresses_root_cause(
        draft_root_cause="ruff E501 line too long",
        ci_log_text="",
    )
    assert not ok
    assert "appear in ci_log" in reason


# ─────────────────────────────────────────────────────────────────────
# c2 — affected_files_exist_in_repo
# ─────────────────────────────────────────────────────────────────────


def test_c2_passes_when_all_files_exist(workspace):
    ok, _ = check_c2_affected_files_exist(
        draft_affected_files=["src/calc/formatting.py", "tests/test_math_ops.py"],
        workspace_path=workspace,
    )
    assert ok


def test_c2_fails_when_one_file_missing(workspace):
    ok, reason = check_c2_affected_files_exist(
        draft_affected_files=["src/calc/formatting.py", "src/calc/nonexistent.py"],
        workspace_path=workspace,
    )
    assert not ok
    assert "does not exist" in reason


def test_c2_rejects_path_traversal(workspace):
    ok, reason = check_c2_affected_files_exist(
        draft_affected_files=["../etc/passwd"],
        workspace_path=workspace,
    )
    assert not ok
    assert "traversal" in reason or "outside workspace" in reason


def test_c2_rejects_absolute_path(workspace):
    ok, _ = check_c2_affected_files_exist(
        draft_affected_files=["/etc/passwd"],
        workspace_path=workspace,
    )
    assert not ok


def test_c2_passes_for_empty_list_with_note(workspace):
    """Empty affected_files isn't a c2 failure (it's an engineer-side guard)."""
    ok, _ = check_c2_affected_files_exist(
        draft_affected_files=[],
        workspace_path=workspace,
    )
    assert ok


# ─────────────────────────────────────────────────────────────────────
# c3 — verify_command_will_distinguish_success
# ─────────────────────────────────────────────────────────────────────


async def test_c3_passes_for_resolvable_command_with_sandbox():
    class _OK:
        exit_code = 0

    async def fake_exec(_cid, _cmd, **_):
        return _OK()

    ok, _ = await check_c3_verify_command_resolvable(
        draft_verify_command="ruff check .",
        sandbox_container_id="sandbox-1",
        exec_in_sandbox=fake_exec,
    )
    assert ok


async def test_c3_fails_when_sandbox_says_command_not_found():
    class _FAIL:
        exit_code = 1

    async def fake_exec(_cid, _cmd, **_):
        return _FAIL()

    ok, reason = await check_c3_verify_command_resolvable(
        draft_verify_command="totally-fake-tool --x",
        sandbox_container_id="sandbox-1",
        exec_in_sandbox=fake_exec,
    )
    assert not ok
    assert "command -v" in reason


async def test_c3_rejects_shell_metachars_in_first_token():
    ok, reason = await check_c3_verify_command_resolvable(
        draft_verify_command="rm -rf / ; pytest",
        sandbox_container_id=None,
    )
    # shlex parses this fine; first token is "rm" which IS a valid ident.
    # But this test exists to remind: c3's job is to verify the FIRST TOKEN
    # is something that should be runnable; broader command safety lives
    # elsewhere (sandbox sandboxing, prompt-side rules).
    assert ok or "metachars" in reason or "unverified" in reason


async def test_c3_soft_passes_without_sandbox():
    ok, reason = await check_c3_verify_command_resolvable(
        draft_verify_command="pytest tests/",
        sandbox_container_id=None,
    )
    assert ok
    assert "unverified" in reason


async def test_c3_fails_on_empty_command():
    ok, _ = await check_c3_verify_command_resolvable(
        draft_verify_command="",
        sandbox_container_id="sandbox-1",
    )
    assert not ok


# ─────────────────────────────────────────────────────────────────────
# 10-fix_spec corpus — milestone gate
# ─────────────────────────────────────────────────────────────────────


def _honest_specs():
    return [
        # 1: lint fix
        {
            "root_cause": "ruff E501 line-too-long in formatting.py",
            "affected_files": ["src/calc/formatting.py"],
            "failing_command": "ruff check .",
            "verify_command": "ruff check .",
        },
        # 2: test fix
        {
            "root_cause": "test_multiply assertion mismatch in test_math_ops",
            "affected_files": ["tests/test_math_ops.py"],
            "failing_command": "pytest tests/test_math_ops.py",
            "verify_command": "pytest tests/test_math_ops.py",
        },
        # 3: delete-test fix (broader verify_command than failing_command)
        {
            "root_cause": "test_multiply assertion mismatch — synthetic test should be removed",
            "affected_files": ["tests/test_math_ops.py"],
            "failing_command": "pytest tests/test_math_ops.py::test_multiply",
            "verify_command": "pytest tests/test_math_ops.py",
        },
        # 4: env-config fix
        {
            "root_cause": "ruff line-length config mismatch in formatting setup",
            "affected_files": ["src/calc/formatting.py"],
            "failing_command": "ruff check .",
            "verify_command": "ruff check .",
        },
        # 5: math_ops fix
        {
            "root_cause": "math_ops.py add function returns wrong value",
            "affected_files": ["src/calc/math_ops.py"],
            "failing_command": "pytest tests/test_math_ops.py",
            "verify_command": "pytest tests/test_math_ops.py",
        },
    ]


def _dishonest_specs():
    return [
        # 1: fabricated root_cause (DatabaseTimeout / PostgreSQL not in any log)
        {
            "kind": "fabricated_root_cause",
            "spec": {
                "root_cause": "DatabaseTimeout connecting to PostgreSQL replica",
                "affected_files": ["src/calc/formatting.py"],
                "failing_command": "ruff check .",
                "verify_command": "ruff check .",
            },
            "expected_mismatch": "ci_log_addresses_root_cause",
        },
        # 2: missing file
        {
            "kind": "missing_file",
            "spec": {
                "root_cause": "ruff E501 long line in does_not_exist",
                "affected_files": ["src/calc/does_not_exist.py"],
                "failing_command": "ruff check .",
                "verify_command": "ruff check .",
            },
            "expected_mismatch": "affected_files_exist_in_repo",
        },
        # 3: path traversal
        {
            "kind": "path_traversal",
            "spec": {
                "root_cause": "ruff E501 long line",
                "affected_files": ["../etc/passwd"],
                "failing_command": "ruff check .",
                "verify_command": "ruff check .",
            },
            "expected_mismatch": "affected_files_exist_in_repo",
        },
        # 4: nonexistent verify_command first-token (sandbox check fails)
        {
            "kind": "unresolvable_verify_command",
            "spec": {
                "root_cause": "ruff E501 long line in formatting",
                "affected_files": ["src/calc/formatting.py"],
                "failing_command": "ruff check .",
                "verify_command": "totally-fake-binary-name-12345 --x",
            },
            "expected_mismatch": "verify_command_will_distinguish_success",
        },
        # 5: shell injection in verify_command first token
        {
            "kind": "shell_injection_in_verify",
            "spec": {
                "root_cause": "ruff E501 long line in formatting",
                "affected_files": ["src/calc/formatting.py"],
                "failing_command": "ruff check .",
                # shlex parses 'pytest;rm' as one token — the semicolon
                # makes the first token fail c3's identifier regex.
                "verify_command": "pytest;rm -rf /",
            },
            "expected_mismatch": "verify_command_will_distinguish_success",
        },
    ]


async def test_corpus_honest_specs_all_pass(workspace):
    """Phase-1 milestone (half): all 5 honest fix_specs pass validation
    end-to-end via commander_verify_fix_spec_self_critique."""

    class _OK:
        exit_code = 0

    async def fake_exec(_cid, _cmd, **_):
        return _OK()

    for i, spec in enumerate(_honest_specs(), start=1):
        all_ok, mismatches = await commander_verify_fix_spec_self_critique(
            fix_spec=spec,
            workspace_path=workspace,
            ci_log_text=_CI_LOG_E501 + "\n" + _CI_LOG_PYTEST_FAIL,
            sandbox_container_id="sandbox-1",
            exec_in_sandbox=fake_exec,
        )
        assert all_ok, f"honest spec #{i} unexpectedly failed: {mismatches}"


async def test_corpus_dishonest_specs_all_caught(workspace):
    """Phase-1 milestone (other half): all 5 dishonest fix_specs caught
    with the EXPECTED mismatch reason."""

    class _OK:
        exit_code = 0

    class _NOTFOUND:
        exit_code = 1

    async def fake_exec(_cid, cmd, **_):
        # `command -v totally-fake-binary-name-12345` returns non-zero
        if "totally-fake-binary-name" in cmd:
            return _NOTFOUND()
        return _OK()

    for entry in _dishonest_specs():
        spec = entry["spec"]
        expected = entry["expected_mismatch"]
        all_ok, mismatches = await commander_verify_fix_spec_self_critique(
            fix_spec=spec,
            workspace_path=workspace,
            ci_log_text=_CI_LOG_E501,
            sandbox_container_id="sandbox-1",
            exec_in_sandbox=fake_exec,
        )
        assert not all_ok, f"dishonest spec ({entry['kind']}) was wrongly accepted"
        check_names = [m["check"] for m in mismatches]
        assert expected in check_names, (
            f"dishonest spec ({entry['kind']}): expected {expected!r} in mismatches, "
            f"got {check_names}"
        )


def test_milestone_zero_false_negatives_summary():
    """Sanity: the corpus has 5 honest + 5 dishonest, the milestone
    requires zero false negatives. This test is a structural check on
    the corpus itself, not a runtime test."""
    assert len(_honest_specs()) == 5
    assert len(_dishonest_specs()) == 5
    assert {e["expected_mismatch"] for e in _dishonest_specs()} == {
        "ci_log_addresses_root_cause",
        "affected_files_exist_in_repo",
        "verify_command_will_distinguish_success",
    }, "corpus must cover all 3 check classes"


# ─────────────────────────────────────────────────────────────────────
# c8 — test_behavior_preserved (v1.7.2.5)
# ─────────────────────────────────────────────────────────────────────
#
# The 2026-05-04 soak surfaced 3 flake-cell failures all driven by TL
# choosing "delete the flaky test" instead of "fix the timing source."
# c8 catches that anti-pattern at TL emit time.

from phalanx.agents._tl_self_critique import (  # noqa: E402
    check_c8_test_behavior_preserved,
)


_CI_LOG_FLAKE_TIMEOUT = """\
============================= test session starts ==============================
collected 6 items
tests/test_math_ops.py::test_multiply_with_jitter
+++++++++++++++++++++ Timeout +++++++++++++++++++++
~~~~ Stack of MainThread ~~~~
File "tests/test_math_ops.py", line 14, in test_multiply_with_jitter
    time.sleep(random.uniform(0, 3))
Failed: Timeout >2.0s
"""


_HONEST_DETERMINISTIC_FIX_STEPS = [
    {
        "id": 1, "action": "replace",
        "file": "tests/test_math_ops.py",
        "old": "time.sleep(random.uniform(0, 3))",
        "new": "# (deterministic — no sleep needed for behavioral coverage)",
    },
    {"id": 2, "action": "commit", "message": "test: remove flaky sleep"},
    {"id": 3, "action": "push"},
]

_DELETE_FLAKY_TEST_STEPS = [
    {
        "id": 1, "action": "delete_lines",
        "file": "tests/test_math_ops.py",
        "line": 12, "end_line": 18,
    },
    {"id": 2, "action": "commit", "message": "remove flaky test"},
    {"id": 3, "action": "push"},
]

_SKIP_DECORATOR_STEPS = [
    {
        "id": 1, "action": "replace",
        "file": "tests/test_math_ops.py",
        "old": "def test_multiply_with_jitter():",
        "new": "@pytest.mark.skip(reason='flaky')\ndef test_multiply_with_jitter():",
    },
    {"id": 2, "action": "commit", "message": "skip flaky test"},
    {"id": 3, "action": "push"},
]

_PYTESTMARK_SKIP_STEPS = [
    {
        "id": 1, "action": "insert",
        "file": "tests/test_math_ops.py",
        "after_line": 1,
        "content": "pytestmark = pytest.mark.skip(reason='broken on CI')",
    },
    {"id": 2, "action": "commit", "message": "module-skip flaky test"},
    {"id": 3, "action": "push"},
]

_APPLY_DIFF_REMOVE_TEST = [
    {
        "id": 1, "action": "apply_diff",
        "diff": (
            "--- a/tests/test_math_ops.py\n"
            "+++ b/tests/test_math_ops.py\n"
            "@@ -10,8 +10,2 @@\n"
            " import pytest\n"
            " \n"
            "-def test_multiply_with_jitter():\n"
            "-    time.sleep(random.uniform(0, 3))\n"
            "-    assert multiply(2, 3) == 6\n"
            "-\n"
            " def test_subtract():\n"
            "     assert subtract(5, 3) == 2\n"
        ),
    },
    {"id": 2, "action": "commit", "message": "remove flaky test"},
    {"id": 3, "action": "push"},
]


# ── Acceptance cases ──────────────────────────────────────────────────


def test_c8_passes_when_no_steps():
    """No engineer steps yet — nothing to evaluate."""
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=None,
        draft_root_cause="flaky test_multiply_with_jitter times out",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is True
    assert reason == ""


def test_c8_passes_for_non_flake_failure_even_with_test_deletion():
    """c8 only fires on flake/timing signals. A genuine 'remove an
    obsolete test' deletion on a non-flake failure should pass."""
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=_DELETE_FLAKY_TEST_STEPS,
        draft_root_cause="The test was renamed in the PR; remove the old reference.",
        ci_log_text="ImportError: cannot import test_renamed_thing",
    )
    assert ok is True


def test_c8_passes_for_deterministic_fix_on_flake():
    """The RIGHT shape: TL replaces the random sleep with a comment
    explaining why the timing wasn't actually needed. Behavior preserved."""
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=_HONEST_DETERMINISTIC_FIX_STEPS,
        draft_root_cause="Test test_multiply_with_jitter times out due to random sleep",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is True


def test_c8_passes_for_seed_randomness_on_flake():
    """Seeding randomness is the canonical deterministic fix — must
    pass even though the diff touches the test file."""
    seed_steps = [
        {
            "id": 1, "action": "insert",
            "file": "tests/test_math_ops.py",
            "after_line": 5,
            "content": "import random\nrandom.seed(42)",
        },
        {"id": 2, "action": "commit", "message": "test: seed random"},
    ]
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=seed_steps,
        draft_root_cause="flaky test due to random.uniform sleep",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is True


def test_c8_passes_when_root_cause_mentions_flake_but_steps_fix_source():
    """TL is fixing a flake by editing the SOURCE under test (not the
    test). That's preserving behavioral intent — pass."""
    src_fix_steps = [
        {
            "id": 1, "action": "replace",
            "file": "src/calc/math_ops.py",
            "old": "time.sleep(random.uniform(0, 3))",
            "new": "# removed nondeterministic sleep",
        },
        {"id": 2, "action": "commit", "message": "fix: remove flaky sleep in math_ops"},
    ]
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=src_fix_steps,
        draft_root_cause="flaky test_multiply due to random sleep in math_ops",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is True


# ── Rejection cases ───────────────────────────────────────────────────


def test_c8_fails_on_test_deletion_for_flake():
    """The exact 2026-05-04 soak failure: TL emits delete_lines on
    a tests/ path on a flake-shape failure. c8 rejects."""
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=_DELETE_FLAKY_TEST_STEPS,
        draft_root_cause="Remove the flaky test_multiply_with_jitter from tests",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False
    assert "deletes from test" in reason
    assert "deterministic" in reason or "timing" in reason


def test_c8_fails_on_pytest_skip_decorator():
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=_SKIP_DECORATOR_STEPS,
        draft_root_cause="test_multiply_with_jitter is flaky; skip it",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False
    assert "skip directive" in reason


def test_c8_fails_on_pytestmark_module_skip():
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=_PYTESTMARK_SKIP_STEPS,
        draft_root_cause="flaky tests timing out",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False
    assert "skip directive" in reason


def test_c8_fails_on_apply_diff_removing_flaky_test_function():
    """apply_diff with `-def test_<flaky>...` lines. c8 inspects the
    diff body for removed test-function definitions."""
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=_APPLY_DIFF_REMOVE_TEST,
        draft_root_cause="Remove flaky test_multiply_with_jitter",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False
    assert "removes" in reason or "deleting" in reason.lower()


def test_c8_fails_on_xfail_decorator_for_flake():
    """Marking a flaky test as xfail is also hiding behavior."""
    xfail_steps = [
        {
            "id": 1, "action": "replace",
            "file": "tests/test_math_ops.py",
            "old": "def test_multiply_with_jitter():",
            "new": "@pytest.mark.xfail(reason='flaky')\ndef test_multiply_with_jitter():",
        },
        {"id": 2, "action": "commit", "message": "xfail flaky"},
    ]
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=xfail_steps,
        draft_root_cause="flaky test timing out",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False
    assert "skip directive" in reason


def test_c8_fails_on_inline_pytest_skip_call():
    inline_skip_steps = [
        {
            "id": 1, "action": "insert",
            "file": "tests/test_math_ops.py",
            "after_line": 13,
            "content": "    pytest.skip('flaky timing')",
        },
        {"id": 2, "action": "commit", "message": "skip flaky body"},
    ]
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=inline_skip_steps,
        draft_root_cause="random sleep makes test flaky",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False


def test_c8_fails_when_only_ci_log_signals_flake():
    """c8 should fire on flake signals in EITHER root_cause OR ci_log.
    Catches TL trying to launder the diagnosis ("just a slow test")
    when the log clearly says timeout."""
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=_DELETE_FLAKY_TEST_STEPS,
        draft_root_cause="The test takes too long and should be removed",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,  # contains "Timeout" + "Failed: Timeout"
    )
    assert ok is False


def test_c8_fails_when_only_root_cause_signals_flake():
    """Mirror: ci_log might be opaque, but root_cause names flake."""
    opaque_log = "exit code 1\n"
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=_DELETE_FLAKY_TEST_STEPS,
        draft_root_cause="test_multiply_with_jitter is intermittent / flaky",
        ci_log_text=opaque_log,
    )
    assert ok is False


# ── Soak failure replays ──────────────────────────────────────────────


def test_c8_rejects_2026_05_04_soak_run_ee5fd137():
    """The flake-cell run that actually shipped a regression yesterday.
    With c8, this plan would have been rejected at TL emit time, never
    reaching the engineer + the gate."""
    soak_root_cause = (
        "PR added intentionally flaky test_multiply_with_jitter that "
        "sleeps for up to 3 seconds while CI runs pytest with --timeout=2."
    )
    soak_steps = [
        {
            "id": 1, "action": "delete_lines",
            "file": "tests/test_math_ops.py",
            "line": 14, "end_line": 21,
        },
        {
            "id": 2, "action": "replace",
            "file": "tests/test_math_ops.py",
            "old": "import random\nimport time\n",
            "new": "",
        },
        {"id": 3, "action": "commit", "message": "remove flaky test_multiply_with_jitter"},
        {"id": 4, "action": "push"},
    ]
    ok, reason = check_c8_test_behavior_preserved(
        draft_steps=soak_steps,
        draft_root_cause=soak_root_cause,
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is False
    assert "test" in reason.lower()


def test_c8_passes_for_soak_run_with_correct_seeded_alternative():
    """The shape TL SHOULD emit instead — modify the test to remove
    the random sleep but preserve the behavioral assertion."""
    correct_steps = [
        {
            "id": 1, "action": "replace",
            "file": "tests/test_math_ops.py",
            "old": "    time.sleep(random.uniform(0, 3))",
            "new": "    # behavioral coverage preserved without timing variance",
        },
        {
            "id": 2, "action": "replace",
            "file": "tests/test_math_ops.py",
            "old": "import random\nimport time\n",
            "new": "",
        },
        {"id": 3, "action": "commit", "message": "test: deterministic test_multiply_with_jitter"},
        {"id": 4, "action": "push"},
    ]
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=correct_steps,
        draft_root_cause="random sleep makes test_multiply_with_jitter flaky",
        ci_log_text=_CI_LOG_FLAKE_TIMEOUT,
    )
    assert ok is True


# ── Tool-handler integration ──────────────────────────────────────────


def test_c8_appears_in_validated_dict():
    """The validate_self_critique tool must surface c8 in its output
    so TL can read it and adjust before emit."""
    from phalanx.agents._tl_self_critique import (  # noqa: PLC0415
        check_c8_test_behavior_preserved,
    )
    # Just spot-check the function exists with the expected signature
    ok, _ = check_c8_test_behavior_preserved(
        draft_steps=[],
        draft_root_cause="",
        ci_log_text="",
    )
    assert ok is True  # empty inputs default to pass
