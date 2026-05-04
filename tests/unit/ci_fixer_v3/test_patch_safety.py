"""Tier-1 tests for v1.7.2.3 patch safety guards.

These guards are the engineer's "don't break the user's repo" line.
They block:
  - CI config edits (.github/workflows, codecov, pre-commit)
  - test deletion / @pytest.skip injection
  - paths outside TL's affected_files allowlist
"""

from __future__ import annotations

from phalanx.agents._patch_safety import validate_step_safety


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_normal_replace_in_src_passes(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "src/calc/formatting.py",
            "old": "x", "new": "y",
        })
        assert v.ok is True

    def test_run_step_passes(self):
        v = validate_step_safety({"id": 1, "action": "run", "command": "pytest -q"})
        assert v.ok is True

    def test_commit_passes(self):
        v = validate_step_safety({"id": 1, "action": "commit", "message": "fix"})
        assert v.ok is True

    def test_push_passes(self):
        v = validate_step_safety({"id": 1, "action": "push"})
        assert v.ok is True


# ─────────────────────────────────────────────────────────────────────────────
# R1 — blocked paths (CI config)
# ─────────────────────────────────────────────────────────────────────────────


class TestBlockedPaths:
    def test_github_workflows_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": ".github/workflows/ci.yml",
            "old": "x", "new": "y",
        })
        assert v.ok is False
        assert v.rule == "blocked_path"
        assert ".github/workflows" in (v.detail or "")

    def test_github_workflows_yaml_extension_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": ".github/workflows/lint.yaml",
            "old": "x", "new": "y",
        })
        assert v.ok is False
        assert v.rule == "blocked_path"

    def test_codecov_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": ".codecov.yml",
            "old": "threshold: 80", "new": "threshold: 0",
        })
        assert v.ok is False
        assert v.rule == "blocked_path"

    def test_codecov_top_level_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "codecov.yml",
            "old": "x", "new": "y",
        })
        assert v.ok is False
        assert v.rule == "blocked_path"

    def test_pre_commit_config_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": ".pre-commit-config.yaml",
            "old": "x", "new": "y",
        })
        assert v.ok is False
        assert v.rule == "blocked_path"

    def test_dependabot_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": ".github/dependabot.yml",
            "old": "x", "new": "y",
        })
        assert v.ok is False
        assert v.rule == "blocked_path"


# ─────────────────────────────────────────────────────────────────────────────
# R2 — allowlist
# ─────────────────────────────────────────────────────────────────────────────


class TestAllowlist:
    def test_no_allowlist_means_all_paths_pass(self):
        v = validate_step_safety(
            {"id": 1, "action": "replace", "file": "anywhere.py",
             "old": "x", "new": "y"},
            allowed_files=None,
        )
        assert v.ok is True

    def test_empty_allowlist_means_all_paths_pass(self):
        """Empty list = no list, mirrors v1.6 fallback when TL doesn't
        declare affected_files."""
        v = validate_step_safety(
            {"id": 1, "action": "replace", "file": "anywhere.py",
             "old": "x", "new": "y"},
            allowed_files=[],
        )
        assert v.ok is True

    def test_path_in_allowlist_passes(self):
        v = validate_step_safety(
            {"id": 1, "action": "replace", "file": "src/foo.py",
             "old": "x", "new": "y"},
            allowed_files=["src/foo.py", "src/bar.py"],
        )
        assert v.ok is True

    def test_path_not_in_allowlist_blocked(self):
        v = validate_step_safety(
            {"id": 1, "action": "replace", "file": "src/baz.py",
             "old": "x", "new": "y"},
            allowed_files=["src/foo.py", "src/bar.py"],
        )
        assert v.ok is False
        assert v.rule == "allowlist_miss"


# ─────────────────────────────────────────────────────────────────────────────
# R3 — test deletion
# ─────────────────────────────────────────────────────────────────────────────


class TestTestDeletion:
    def test_delete_lines_in_test_file_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "delete_lines", "file": "tests/test_foo.py",
            "start": 1, "end": 100,
        })
        assert v.ok is False
        assert v.rule == "test_deletion"

    def test_delete_lines_test_underscore_prefix_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "delete_lines", "file": "test_foo.py",
            "start": 1, "end": 100,
        })
        assert v.ok is False
        assert v.rule == "test_deletion"

    def test_delete_lines_underscore_test_suffix_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "delete_lines", "file": "src/foo_test.py",
            "start": 1, "end": 100,
        })
        assert v.ok is False
        assert v.rule == "test_deletion"

    def test_delete_lines_in_src_passes(self):
        """Deleting from non-test code is fine (e.g. removing a buggy method)."""
        v = validate_step_safety({
            "id": 1, "action": "delete_lines", "file": "src/foo.py",
            "start": 1, "end": 5,
        })
        assert v.ok is True

    def test_replace_in_test_file_passes(self):
        """Editing a test (e.g. fixing an assertion) is allowed —
        only outright deletion is blocked. Skip-injection caught by R4."""
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "tests/test_foo.py",
            "old": "assert x == 1", "new": "assert x == 2",
        })
        assert v.ok is True


# ─────────────────────────────────────────────────────────────────────────────
# R4 — skip-directive injection
# ─────────────────────────────────────────────────────────────────────────────


class TestSkipInjection:
    def test_pytest_skip_decorator_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "tests/test_foo.py",
            "old": "def test_thing():",
            "new": "@pytest.mark.skip\ndef test_thing():",
        })
        assert v.ok is False
        assert v.rule == "skip_injection"

    def test_pytest_skipif_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "tests/test_foo.py",
            "old": "def test_thing():",
            "new": "@pytest.mark.skipif(True, reason='broken')\ndef test_thing():",
        })
        assert v.ok is False
        assert v.rule == "skip_injection"

    def test_pytest_xfail_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "tests/test_foo.py",
            "old": "def test_thing():",
            "new": "@pytest.mark.xfail\ndef test_thing():",
        })
        assert v.ok is False
        assert v.rule == "skip_injection"

    def test_unittest_skip_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "tests/test_foo.py",
            "old": "def test_thing(self):",
            "new": "@unittest.skip('flaky')\ndef test_thing(self):",
        })
        assert v.ok is False
        assert v.rule == "skip_injection"

    def test_inline_pytest_skip_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace", "file": "tests/test_foo.py",
            "old": "x = 1", "new": "pytest.skip('TODO')",
        })
        assert v.ok is False
        assert v.rule == "skip_injection"

    def test_pytestmark_skip_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "insert", "file": "tests/test_foo.py",
            "after_line": 1,
            "content": "pytestmark = pytest.mark.skip(reason='broken')",
        })
        assert v.ok is False
        assert v.rule == "skip_injection"


# ─────────────────────────────────────────────────────────────────────────────
# R5 — apply_diff smuggling
# ─────────────────────────────────────────────────────────────────────────────


class TestApplyDiffGuards:
    def test_diff_touching_workflow_blocked(self):
        diff = (
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1,3 +1,3 @@\n"
            "-on: [push]\n"
            "+on: [pull_request]\n"
            " jobs:\n"
            "   test:\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is False
        assert v.rule == "workflow_in_diff"

    def test_diff_adding_skip_directive_blocked(self):
        diff = (
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+@pytest.mark.skip\n"
            " def test_thing():\n"
            "     assert True\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is False
        assert v.rule == "skip_injection"

    def test_diff_in_src_passes(self):
        diff = (
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is True


# ─────────────────────────────────────────────────────────────────────────────
# Integration: the step interpreter dispatches through validate_step_safety
# ─────────────────────────────────────────────────────────────────────────────


class TestStepInterpreterIntegration:
    def test_blocked_path_short_circuits_handler(self, tmp_path):
        from phalanx.agents._engineer_step_interpreter import execute_step

        result = execute_step(
            {"id": 1, "action": "replace", "file": ".github/workflows/ci.yml",
             "old": "x", "new": "y"},
            tmp_path,
        )
        assert result.ok is False
        assert "patch_safety_violation" in (result.error or "")
        assert "blocked_path" in (result.error or "")

    def test_allowlist_violation_short_circuits(self, tmp_path):
        from phalanx.agents._engineer_step_interpreter import execute_step

        # Create a real file so handler-level checks don't mask the gate
        target = tmp_path / "src" / "outside.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n")

        result = execute_step(
            {"id": 1, "action": "replace", "file": "src/outside.py",
             "old": "x = 1", "new": "x = 2"},
            tmp_path,
            allowed_files=["src/inside.py"],  # different file
        )
        assert result.ok is False
        assert "allowlist_miss" in (result.error or "")

    def test_safe_step_passes_through_to_handler(self, tmp_path):
        from phalanx.agents._engineer_step_interpreter import execute_step

        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n")

        result = execute_step(
            {"id": 1, "action": "replace", "file": "src/foo.py",
             "old": "x = 1", "new": "x = 2"},
            tmp_path,
            allowed_files=["src/foo.py"],
        )
        assert result.ok is True
        assert target.read_text() == "x = 2\n"


# ─────────────────────────────────────────────────────────────────────────────
# v1.7.2.6 — R6 (assertion_reduction) + R7 (test_function_reduction)
# ─────────────────────────────────────────────────────────────────────────────


class TestAssertionReductionR6:
    """R6 — replace/apply_diff that drops assertions on a test file
    must be blocked unless step.allow_test_reduction=True."""

    def test_replace_keeping_same_assert_count_passes(self):
        """Mutating an assertion (==1 → ==2) keeps coverage equivalent."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "assert x == 1",
            "new": "assert x == 2",
        })
        assert v.ok is True

    def test_replace_increasing_assert_count_passes(self):
        """Adding more checks is always fine."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "assert x == 1",
            "new": "assert x == 1\nassert y == 2",
        })
        assert v.ok is True

    def test_replace_dropping_assertion_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "assert x == 1\nassert y == 2",
            "new": "assert x == 1",
        })
        assert v.ok is False
        assert v.rule == "assertion_reduction"
        assert "(2 → 1)" in (v.detail or "")

    def test_replace_dropping_all_assertions_blocked(self):
        """Replacing assertions with `pass` is the worst case."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "assert x == 1\nassert y == 2\nassert z == 3",
            "new": "pass",
        })
        assert v.ok is False
        assert v.rule == "assertion_reduction"

    def test_unittest_assert_methods_count(self):
        """assertEqual, assertTrue etc. count as assertions for R6."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "self.assertEqual(x, 1)\nself.assertTrue(y)",
            "new": "self.assertEqual(x, 1)",  # dropped assertTrue
        })
        assert v.ok is False
        assert v.rule == "assertion_reduction"

    def test_assertion_replaced_with_unittest_form_passes(self):
        """`assert x == 1` ↔ `self.assertEqual(x, 1)` is equivalent
        coverage; counts both patterns so passes."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "assert x == 1",
            "new": "self.assertEqual(x, 1)",
        })
        assert v.ok is True

    def test_explicit_allow_test_reduction_bypasses_r6(self):
        """TL can explicitly opt in to a reduction (e.g. consolidating
        duplicate assertions). Engineer never sets this — only TL does
        in its task_plan."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "assert x == 1\nassert x == 1\nassert x == 1",  # dup
            "new": "assert x == 1",
            "allow_test_reduction": True,
        })
        assert v.ok is True

    def test_non_test_file_not_subject_to_r6(self):
        """R6 only applies to test paths. Production code can drop
        `assert` statements freely (they're often runtime checks)."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "src/foo.py",
            "old": "assert config.valid\nassert ready",
            "new": "if not config.valid:\n    raise RuntimeError()",
        })
        assert v.ok is True


class TestTestFunctionReductionR7:
    """R7 — replace/apply_diff that removes `def test_<name>` blocks
    on a test file must be blocked unless allow_test_reduction=True."""

    def test_replace_keeping_test_funcs_passes(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "def test_a():\n    assert True",
            "new": "def test_a():\n    assert x == 1",  # same func, modified body
        })
        assert v.ok is True

    def test_replace_adding_test_func_passes(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "def test_a():\n    assert True",
            "new": "def test_a():\n    assert True\n\ndef test_b():\n    assert True",
        })
        assert v.ok is True

    def test_replace_removing_test_func_blocked(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "def test_a():\n    assert True\n\ndef test_b():\n    assert True",
            "new": "def test_a():\n    assert True",  # test_b removed
        })
        assert v.ok is False
        assert v.rule == "test_function_reduction"
        assert "(2 → 1)" in (v.detail or "")

    def test_class_method_test_funcs_counted(self):
        """class-method tests `def test_method(self):` (with leading
        whitespace) count via MULTILINE."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": (
                "class TestThing:\n"
                "    def test_a(self):\n"
                "        pass\n"
                "    def test_b(self):\n"
                "        pass\n"
            ),
            "new": (
                "class TestThing:\n"
                "    def test_a(self):\n"
                "        pass\n"
            ),
        })
        assert v.ok is False
        assert v.rule == "test_function_reduction"

    def test_explicit_allow_test_reduction_bypasses_r7(self):
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_foo.py",
            "old": "def test_a():\n    pass\n\ndef test_b():\n    pass",
            "new": "def test_a():\n    pass",
            "allow_test_reduction": True,
        })
        assert v.ok is True

    def test_non_test_file_not_subject_to_r7(self):
        """Removing functions named `test_*` from non-test files passes —
        e.g. utility functions in production code."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "src/utils.py",
            "old": "def test_helper():\n    pass\n\ndef other():\n    pass",
            "new": "def other():\n    pass",
        })
        assert v.ok is True


class TestApplyDiffTestPreservation:
    """R6/R7 also fire on apply_diff bodies that reduce coverage on
    test files. The diff's `+++ b/<path>` line determines if the target
    is a test path."""

    def test_diff_adding_assertions_passes(self):
        diff = (
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,3 +1,5 @@\n"
            " def test_x():\n"
            "     assert x == 1\n"
            "+    assert x > 0\n"
            "+    assert isinstance(x, int)\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is True

    def test_diff_dropping_assertions_blocked(self):
        diff = (
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,4 +1,2 @@\n"
            " def test_x():\n"
            "     assert x == 1\n"
            "-    assert x > 0\n"
            "-    assert isinstance(x, int)\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is False
        assert v.rule == "assertion_reduction"
        assert "tests/test_foo.py" in (v.detail or "")

    def test_diff_removing_test_function_blocked(self):
        diff = (
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -10,7 +10,2 @@\n"
            " import pytest\n"
            " \n"
            "-def test_b():\n"
            "-    assert True\n"
            "-\n"
            "-def test_c():\n"
            "-    assert False\n"
            " def test_a():\n"
            "     assert True\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is False
        # Either rule is acceptable (both fire on this shape) — the diff
        # removes 2 funcs + 2 asserts; whichever check trips first wins.
        assert v.rule in {"test_function_reduction", "assertion_reduction"}

    def test_diff_explicit_allow_bypasses(self):
        """TL escalation-approves a test removal via allow_test_reduction."""
        diff = (
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,5 +1,2 @@\n"
            "-def test_obsolete():\n"
            "-    # superseded by test_new in another file\n"
            "-    assert True\n"
            " def test_other():\n"
            "     pass\n"
        )
        v = validate_step_safety({
            "id": 1, "action": "apply_diff", "diff": diff,
            "allow_test_reduction": True,
        })
        assert v.ok is True

    def test_diff_on_non_test_file_passes_freely(self):
        diff = (
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,3 +1,1 @@\n"
            "-assert config.valid\n"
            "-assert ready\n"
            "-def test_helper():\n"
            "+def helper():\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is True


class TestSoakReplayR6R7:
    """Replays the 2026-05-04 soak's flake-cell `replace` shape that
    removed test imports + the test body. With R6+R7, this would have
    been blocked at the engineer step interpreter even if c8 missed it."""

    def test_soak_flake_replay_remove_test_body_blocked(self):
        """The shape from soak run ee5fd137: a `replace` whose `old` is
        the full flaky test definition + body and `new` is empty.
        Should be blocked by R7 (1 → 0 test_func) and R6
        (≥1 → 0 asserts)."""
        v = validate_step_safety({
            "id": 1, "action": "replace",
            "file": "tests/test_math_ops.py",
            "old": (
                "def test_multiply_with_jitter():\n"
                "    time.sleep(random.uniform(0, 3))\n"
                "    assert multiply(2, 3) == 6\n"
            ),
            "new": "",
        })
        assert v.ok is False
        # Either rule is fine — both should catch it
        assert v.rule in {"test_function_reduction", "assertion_reduction"}

    def test_soak_flake_replay_diff_form_blocked(self):
        """Same shape but emitted as apply_diff."""
        diff = (
            "--- a/tests/test_math_ops.py\n"
            "+++ b/tests/test_math_ops.py\n"
            "@@ -10,5 +10,1 @@\n"
            " import pytest\n"
            "-def test_multiply_with_jitter():\n"
            "-    time.sleep(random.uniform(0, 3))\n"
            "-    assert multiply(2, 3) == 6\n"
            "-\n"
            " def test_other():\n"
        )
        v = validate_step_safety({"id": 1, "action": "apply_diff", "diff": diff})
        assert v.ok is False
        assert v.rule in {"test_function_reduction", "assertion_reduction"}
