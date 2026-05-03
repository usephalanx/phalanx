"""Tier-1 unit tests for v1.7 engineer step interpreter.

Each step action gets:
  - happy path (action succeeds, returns expected output)
  - ≥1 sad path (precondition violated, file missing, etc.)

Plus integration tests for execute_task_steps walking a multi-step plan
and stopping at first failure.

Uses real git in tempdirs — no Docker, no Postgres, no LLM.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from phalanx.agents._engineer_step_interpreter import (
    StepResult,
    execute_step,
    execute_task_steps,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace():
    """Tempdir with a single Python file checked into git."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "src.py").write_text("def hello():\n    return 'world'\n\n# trailer\n")
        subprocess.run(["git", "init", "--quiet"], cwd=str(ws), check=True)
        subprocess.run(["git", "config", "user.email", "t@l"], cwd=str(ws), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"], cwd=str(ws), check=True
        )
        yield ws


# ─── read ────────────────────────────────────────────────────────────────────


class TestRead:
    def test_happy_path(self, workspace):
        r = execute_step({"id": 1, "action": "read", "file": "src.py"}, workspace)
        assert r.ok
        assert r.output["len_bytes"] > 0

    def test_missing_file(self, workspace):
        r = execute_step({"id": 1, "action": "read", "file": "no.py"}, workspace)
        assert not r.ok
        assert r.error == "file_missing"

    def test_path_traversal_rejected(self, workspace):
        r = execute_step({"id": 1, "action": "read", "file": "../etc/passwd"}, workspace)
        assert not r.ok
        assert r.error == "bad_file_path"


# ─── replace ─────────────────────────────────────────────────────────────────


class TestReplace:
    def test_happy_path_modifies_file(self, workspace):
        r = execute_step(
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "return 'world'", "new": "return 'galaxy'"},
            workspace,
        )
        assert r.ok
        assert "galaxy" in (workspace / "src.py").read_text()

    def test_old_text_not_present_caught(self, workspace):
        """The c5/Bug #17 trap: TL emits stale `old` that doesn't exist."""
        r = execute_step(
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "return 'never_in_file'", "new": "return 'x'"},
            workspace,
        )
        assert not r.ok
        assert r.error == "step_precondition_violated"
        assert "old` substring not found" in r.detail

    def test_empty_old_rejected(self, workspace):
        r = execute_step(
            {"id": 1, "action": "replace", "file": "src.py", "old": "", "new": "x"},
            workspace,
        )
        assert not r.ok
        assert r.error == "bad_old"

    def test_new_must_be_string(self, workspace):
        r = execute_step(
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "world", "new": None},
            workspace,
        )
        assert not r.ok
        assert r.error == "bad_new"


# ─── insert ──────────────────────────────────────────────────────────────────


class TestInsert:
    def test_inserts_after_line(self, workspace):
        r = execute_step(
            {"id": 1, "action": "insert", "file": "src.py",
             "after_line": 2, "content": "    print('inserted')"},
            workspace,
        )
        assert r.ok
        contents = (workspace / "src.py").read_text()
        assert "print('inserted')" in contents

    def test_after_line_beyond_file_caught(self, workspace):
        r = execute_step(
            {"id": 1, "action": "insert", "file": "src.py",
             "after_line": 9999, "content": "x"},
            workspace,
        )
        assert not r.ok
        assert r.error == "step_precondition_violated"

    def test_negative_after_line_rejected(self, workspace):
        r = execute_step(
            {"id": 1, "action": "insert", "file": "src.py",
             "after_line": -1, "content": "x"},
            workspace,
        )
        assert not r.ok
        assert r.error == "bad_after_line"


# ─── delete_lines ────────────────────────────────────────────────────────────


class TestDeleteLines:
    def test_deletes_inclusive_range(self, workspace):
        original = (workspace / "src.py").read_text().splitlines()
        # delete lines 1..2 (1-indexed inclusive)
        r = execute_step(
            {"id": 1, "action": "delete_lines", "file": "src.py",
             "line": 1, "end_line": 2},
            workspace,
        )
        assert r.ok
        new_lines = (workspace / "src.py").read_text().splitlines()
        assert len(new_lines) == len(original) - 2

    def test_end_before_start_rejected(self, workspace):
        r = execute_step(
            {"id": 1, "action": "delete_lines", "file": "src.py",
             "line": 5, "end_line": 2},
            workspace,
        )
        assert not r.ok
        assert r.error == "bad_end_line"

    def test_end_beyond_file_caught(self, workspace):
        r = execute_step(
            {"id": 1, "action": "delete_lines", "file": "src.py",
             "line": 1, "end_line": 9999},
            workspace,
        )
        assert not r.ok
        assert r.error == "step_precondition_violated"


# ─── apply_diff ──────────────────────────────────────────────────────────────


class TestApplyDiff:
    def test_applies_clean_diff(self, workspace):
        # Hunk header must reflect the full extent of the file at the
        # patched location, OR git apply rejects with "patch does not apply"
        # because the surrounding context doesn't match. Mirrors what
        # `git diff` would naturally produce.
        diff = (
            "--- a/src.py\n"
            "+++ b/src.py\n"
            "@@ -1,4 +1,4 @@\n"
            " def hello():\n"
            "-    return 'world'\n"
            "+    return 'galaxy'\n"
            " \n"
            " # trailer\n"
        )
        r = execute_step({"id": 1, "action": "apply_diff", "diff": diff}, workspace)
        assert r.ok, f"apply_diff failed: {r.detail}"
        assert "galaxy" in (workspace / "src.py").read_text()

    def test_creates_new_file_via_diff(self, workspace):
        diff = (
            "diff --git a/new_file.py b/new_file.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new_file.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+# brand new file\n"
            "+x = 1\n"
        )
        r = execute_step({"id": 1, "action": "apply_diff", "diff": diff}, workspace)
        assert r.ok
        assert (workspace / "new_file.py").is_file()

    def test_malformed_diff_caught(self, workspace):
        r = execute_step(
            {"id": 1, "action": "apply_diff",
             "diff": "this is not a unified diff"},
            workspace,
        )
        assert not r.ok
        assert r.error == "diff_apply_check_failed"

    def test_diff_against_nonexistent_file_caught(self, workspace):
        diff = (
            "--- a/never_existed.py\n"
            "+++ b/never_existed.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        r = execute_step({"id": 1, "action": "apply_diff", "diff": diff}, workspace)
        assert not r.ok
        assert r.error == "diff_apply_check_failed"


# ─── run ─────────────────────────────────────────────────────────────────────


class TestRun:
    def test_happy_path(self, workspace):
        r = execute_step(
            {"id": 1, "action": "run", "command": "python --version", "expect_exit": 0},
            workspace,
        )
        assert r.ok
        assert "Python" in r.output["stdout_tail"] + r.output["stderr_tail"]

    def test_unexpected_exit_caught(self, workspace):
        r = execute_step(
            {"id": 1, "action": "run", "command": "false", "expect_exit": 0},
            workspace,
        )
        assert not r.ok
        assert r.error == "run_unexpected_exit"

    def test_expected_nonzero_exit_passes(self, workspace):
        r = execute_step(
            {"id": 1, "action": "run", "command": "false", "expect_exit": 1},
            workspace,
        )
        assert r.ok

    def test_expect_stdout_substring(self, workspace):
        r = execute_step(
            {"id": 1, "action": "run", "command": "echo specific_token",
             "expect_exit": 0, "expect_stdout_contains": "specific_token"},
            workspace,
        )
        assert r.ok

    def test_expect_stdout_substring_missing_caught(self, workspace):
        r = execute_step(
            {"id": 1, "action": "run", "command": "echo x",
             "expect_exit": 0, "expect_stdout_contains": "specific_token"},
            workspace,
        )
        assert not r.ok
        assert r.error == "run_stdout_mismatch"

    def test_command_not_found_caught(self, workspace):
        r = execute_step(
            {"id": 1, "action": "run", "command": "definitely_not_a_real_binary_xyz_123"},
            workspace,
        )
        assert not r.ok
        assert r.error == "command_not_found"


# ─── commit ──────────────────────────────────────────────────────────────────


class TestCommit:
    def test_happy_path_returns_sha(self, workspace):
        # Modify the file first so there's something to commit
        (workspace / "src.py").write_text("modified\n")
        r = execute_step(
            {"id": 1, "action": "commit", "message": "test commit"},
            workspace,
        )
        assert r.ok
        sha = r.output["commit_sha"]
        assert sha and len(sha) >= 40

    def test_nothing_to_commit_is_soft_success(self, workspace):
        """Empty commit attempt returns ok=True with note='nothing_to_commit'.
        Engineer continues — TL's diff already landed in a prior step.
        """
        r = execute_step(
            {"id": 1, "action": "commit", "message": "nothing changed"},
            workspace,
        )
        assert r.ok
        assert r.output.get("note") == "nothing_to_commit"
        assert r.output.get("commit_sha") is None

    def test_empty_message_rejected(self, workspace):
        r = execute_step(
            {"id": 1, "action": "commit", "message": ""},
            workspace,
        )
        assert not r.ok
        assert r.error == "bad_message"


# ─── push ────────────────────────────────────────────────────────────────────


class TestPush:
    def test_no_remote_caught(self, workspace):
        """Tempdir repo has no remote; push must fail cleanly with a
        named error, not crash."""
        r = execute_step({"id": 1, "action": "push"}, workspace)
        assert not r.ok
        assert r.error == "git_push_failed"


# ─── unknown / bad action ────────────────────────────────────────────────────


class TestUnknownAction:
    def test_unknown_action_caught(self, workspace):
        r = execute_step({"id": 1, "action": "yeet", "file": "src.py"}, workspace)
        assert not r.ok
        assert r.error == "unknown_action"

    def test_bad_workspace_caught(self):
        r = execute_step(
            {"id": 1, "action": "read", "file": "src.py"},
            "/nonexistent/path/that/does/not/exist",
        )
        assert not r.ok
        assert r.error == "bad_workspace"


# ─── execute_task_steps integration ──────────────────────────────────────────


class TestExecuteTaskSteps:
    def test_walks_steps_in_order_and_stops_at_first_failure(self, workspace):
        steps = [
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "return 'world'", "new": "return 'galaxy'"},
            # This step's `old` won't match because step 1 already changed it
            {"id": 2, "action": "replace", "file": "src.py",
             "old": "return 'world'", "new": "return 'twice'"},
            {"id": 3, "action": "commit", "message": "should never run"},
        ]
        result = execute_task_steps(steps, workspace)
        assert not result.ok
        assert result.completed_steps == [1]
        assert result.failed_step is not None
        assert result.failed_step.step_id == 2
        assert result.failed_step.error == "step_precondition_violated"

    def test_full_pipeline_replace_commit_yields_sha(self, workspace):
        steps = [
            {"id": 1, "action": "replace", "file": "src.py",
             "old": "return 'world'", "new": "return 'galaxy'"},
            {"id": 2, "action": "commit", "message": "swap world→galaxy"},
        ]
        result = execute_task_steps(steps, workspace)
        assert result.ok
        assert result.completed_steps == [1, 2]
        assert result.commit_sha is not None
        assert len(result.commit_sha) >= 40

    def test_apply_diff_then_commit(self, workspace):
        steps = [
            {"id": 1, "action": "apply_diff", "diff": (
                "--- a/src.py\n+++ b/src.py\n"
                "@@ -1,4 +1,4 @@\n"
                " def hello():\n-    return 'world'\n+    return 'patched'\n"
                " \n # trailer\n"
            )},
            {"id": 2, "action": "commit", "message": "patch via diff"},
        ]
        result = execute_task_steps(steps, workspace)
        assert result.ok, f"task steps failed: {result.failed_step}"
        assert "patched" in (workspace / "src.py").read_text()
        assert result.commit_sha is not None
