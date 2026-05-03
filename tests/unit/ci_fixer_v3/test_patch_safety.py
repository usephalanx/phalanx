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
