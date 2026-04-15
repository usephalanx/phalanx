"""
Unit tests for CI Fixer — classifier, log_fetcher helpers, events, command parser.
No DB, no network, no Celery.
"""

from __future__ import annotations

from phalanx.ci_fixer.classifier import classify_failure, extract_failing_files
from phalanx.ci_fixer.events import CIFailureEvent
from phalanx.ci_fixer.log_fetcher import _extract_failure_section, _truncate
from phalanx.gateway.command_parser import CommandType, parse_command

# ── CIFailureEvent ─────────────────────────────────────────────────────────────


class TestCIFailureEvent:
    def test_defaults(self):
        e = CIFailureEvent(
            provider="github_actions",
            repo_full_name="acme/backend",
            branch="main",
            commit_sha="abc123",
            build_id="42",
            build_url="https://github.com/acme/backend/actions/runs/42",
        )
        assert e.pr_number is None
        assert e.failed_jobs == []
        assert e.raw_payload == {}

    def test_all_fields(self):
        e = CIFailureEvent(
            provider="buildkite",
            repo_full_name="acme/api",
            branch="fix/auth",
            commit_sha="deadbeef",
            build_id="bk-99",
            build_url="https://buildkite.com/acme/api/builds/99",
            failed_jobs=["unit-tests", "lint"],
            pr_number=17,
        )
        assert e.provider == "buildkite"
        assert e.pr_number == 17
        assert len(e.failed_jobs) == 2


# ── classify_failure ───────────────────────────────────────────────────────────


class TestClassifyFailure:
    def test_pytest_failure(self):
        log = "FAILED tests/unit/test_auth.py::TestLogin::test_invalid_token"
        assert classify_failure(log) == "test"

    def test_jest_failure(self):
        log = "FAIL src/components/Login.test.tsx\n● Test Suites: 1 failed"
        assert classify_failure(log) == "test"

    def test_assertion_error(self):
        log = "AssertionError: assert 200 == 404"
        assert classify_failure(log) == "test"

    def test_mypy_type_error(self):
        log = "src/api.py:42: error: Argument of type 'str' cannot be assigned to parameter"
        assert classify_failure(log) == "type"

    def test_tsc_type_error(self):
        log = "src/app.tsx(10,5): error TS2345: Argument of type 'number' is not assignable"
        assert classify_failure(log) == "type"

    def test_ruff_lint(self):
        log = "src/foo.py:10:5: E501 line too long (92 > 79 characters)\nFound 3 errors."
        assert classify_failure(log) == "lint"

    def test_eslint_error(self):
        log = "eslint: error  'x' is defined but never used  no-unused-vars"
        assert classify_failure(log) == "lint"

    def test_import_error(self):
        log = "ModuleNotFoundError: No module named 'phalanx.ci_fixer'"
        assert classify_failure(log) == "build"

    def test_syntax_error(self):
        log = "SyntaxError: invalid syntax (foo.py, line 42)"
        assert classify_failure(log) == "build"

    def test_npm_resolution_error(self):
        log = "npm ERR! code ERESOLVE\nnpm ERR! Could not resolve dependency"
        assert classify_failure(log) == "dependency"

    def test_unknown(self):
        log = "Some random build output with no recognizable pattern"
        assert classify_failure(log) == "unknown"

    def test_lint_takes_priority_over_test(self):
        # Log has both a lint error AND test failures — lint wins (checked first)
        log = "Found 1 error.\nFAILED tests/test_foo.py"
        assert classify_failure(log) == "lint"

    def test_empty_log(self):
        assert classify_failure("") == "unknown"

    def test_case_insensitive(self):
        assert classify_failure("MODULENOTFOUNDERROR: no module named 'x'") == "build"


# ── extract_failing_files ──────────────────────────────────────────────────────


class TestExtractFailingFiles:
    def test_pytest_path(self):
        log = "FAILED tests/unit/test_auth.py::TestClass::test_method"
        files = extract_failing_files(log)
        assert "tests/unit/test_auth.py" in files

    def test_mypy_path(self):
        log = "src/api/routes.py:42: error: Argument of type 'str'"
        files = extract_failing_files(log)
        assert "src/api/routes.py" in files

    def test_jest_path(self):
        log = "FAIL src/components/Login.test.tsx"
        files = extract_failing_files(log)
        assert "src/components/Login.test.tsx" in files

    def test_tsc_path(self):
        log = "src/app.tsx(10,5): error TS2345:"
        files = extract_failing_files(log)
        assert "src/app.tsx" in files

    def test_deduplication(self):
        log = "FAILED tests/test_foo.py::TestA::test_1\nFAILED tests/test_foo.py::TestA::test_2\n"
        files = extract_failing_files(log)
        assert files.count("tests/test_foo.py") == 1

    def test_max_10_files(self):
        lines = [f"FAILED tests/test_{i}.py::test" for i in range(20)]
        files = extract_failing_files("\n".join(lines))
        assert len(files) <= 10

    def test_empty_log(self):
        assert extract_failing_files("") == []


# ── log_fetcher helpers ────────────────────────────────────────────────────────


class TestExtractFailureSection:
    def test_finds_error_line(self):
        lines = ["ok", "ok", "Error: something broke", "more output", "done"]
        result = _extract_failure_section(lines)
        assert "Error: something broke" in result

    def test_includes_context_before(self):
        lines = ["context line", "Error: broke"]
        result = _extract_failure_section(lines)
        assert "context line" in result

    def test_fallback_last_lines_when_no_error(self):
        lines = [f"line {i}" for i in range(200)]
        result = _extract_failure_section(lines)
        assert "line 199" in result

    def test_empty_lines(self):
        result = _extract_failure_section([])
        assert result == ""


class TestTruncate:
    def test_short_text_unchanged(self):
        text = "short log"
        assert _truncate(text) == text

    def test_long_text_truncated(self):
        text = "x" * 10000
        result = _truncate(text)
        assert len(result) < len(text)
        assert "truncated" in result

    def test_truncated_keeps_tail(self):
        text = "start " + "x" * 10000 + " end_marker"
        result = _truncate(text)
        assert "end_marker" in result


# ── command parser — /phalanx fix ──────────────────────────────────────────────


class TestParseFix:
    def test_fix_pr_number(self):
        cmd = parse_command("fix acme/backend#42")
        assert cmd.command_type == CommandType.FIX
        assert cmd.fix_repo == "acme/backend"
        assert cmd.fix_pr_number == 42
        assert cmd.fix_branch is None
        assert cmd.is_valid

    def test_fix_branch(self):
        cmd = parse_command("fix acme/api fix/auth-bug")
        assert cmd.command_type == CommandType.FIX
        assert cmd.fix_repo == "acme/api"
        assert cmd.fix_branch == "fix/auth-bug"
        assert cmd.fix_pr_number is None
        assert cmd.is_valid

    def test_fix_no_args(self):
        cmd = parse_command("fix")
        assert cmd.command_type == CommandType.FIX
        assert not cmd.is_valid
        assert "Usage" in cmd.parse_error

    def test_fix_bad_format(self):
        cmd = parse_command("fix not-a-valid-target")
        assert cmd.command_type == CommandType.FIX
        assert not cmd.is_valid

    def test_build_still_works(self):
        cmd = parse_command("build a React todo app")
        assert cmd.command_type == CommandType.BUILD
        assert cmd.title == "a React todo app"
        assert cmd.is_valid

    def test_fix_does_not_affect_build(self):
        # Ensure adding fix command didn't break build parsing
        for title in ["fix the login bug", "fix broken payments", "fix it"]:
            cmd = parse_command(f"build {title}")
            assert cmd.command_type == CommandType.BUILD
            assert cmd.title == title


# ── CIFixerAgent — pure helpers (no DB, no network) ───────────────────────────


class TestCIFixerAgentHelpers:
    def _make_agent(self):
        from phalanx.agents.ci_fixer import CIFixerAgent

        return CIFixerAgent(ci_fix_run_id="00000000-0000-0000-0000-000000000001")

    def test_decrypt_key_passthrough(self):
        agent = self._make_agent()
        assert agent._decrypt_key("my-api-key") == "my-api-key"

    def test_apply_fix_files_writes_files(self, tmp_path):
        agent = self._make_agent()
        files = [{"path": "src/foo.py", "content": "x = 1\n"}]
        written = agent._apply_fix_files(tmp_path, files)
        assert written == ["src/foo.py"]
        assert (tmp_path / "src" / "foo.py").read_text() == "x = 1\n"

    def test_apply_fix_files_skips_empty_path(self, tmp_path):
        agent = self._make_agent()
        written = agent._apply_fix_files(tmp_path, [{"path": "", "content": "x"}])
        assert written == []

    def test_apply_fix_files_skips_empty_content(self, tmp_path):
        agent = self._make_agent()
        written = agent._apply_fix_files(tmp_path, [{"path": "foo.py", "content": ""}])
        assert written == []

    def test_apply_fix_files_creates_nested_dirs(self, tmp_path):
        agent = self._make_agent()
        files = [{"path": "a/b/c/deep.py", "content": "pass\n"}]
        written = agent._apply_fix_files(tmp_path, files)
        assert "a/b/c/deep.py" in written
        assert (tmp_path / "a" / "b" / "c" / "deep.py").exists()

    def test_apply_fix_files_max_10_files(self, tmp_path):
        agent = self._make_agent()
        files = [{"path": f"f{i}.py", "content": "x"} for i in range(15)]
        # All 15 are written — cap is on read, not write
        written = agent._apply_fix_files(tmp_path, files)
        assert len(written) == 15

    def test_read_files_reads_existing(self, tmp_path):
        agent = self._make_agent()
        (tmp_path / "foo.py").write_text("print('hello')")
        result = agent._read_files(tmp_path, ["foo.py"])
        assert "print('hello')" in result

    def test_read_files_skips_missing(self, tmp_path):
        agent = self._make_agent()
        result = agent._read_files(tmp_path, ["does_not_exist.py"])
        assert "no files found" in result

    def test_read_files_empty_list(self, tmp_path):
        agent = self._make_agent()
        assert "no files found" in agent._read_files(tmp_path, [])

    def test_read_files_truncates_large_file(self, tmp_path):
        agent = self._make_agent()
        (tmp_path / "big.py").write_text("x" * 10000)
        result = agent._read_files(tmp_path, ["big.py"])
        assert "truncated" in result


# ── CI_FIXER_SOUL ──────────────────────────────────────────────────────────────


class TestCIFixerSoul:
    def test_soul_registered(self):
        from phalanx.agents.soul import CI_FIXER_SOUL, get_soul

        assert get_soul("ci_fixer") == CI_FIXER_SOUL

    def test_soul_mentions_never_change_tests(self):
        from phalanx.agents.soul import CI_FIXER_SOUL

        assert "test" in CI_FIXER_SOUL.lower()

    def test_soul_mentions_surgical(self):
        from phalanx.agents.soul import CI_FIXER_SOUL

        assert "surgical" in CI_FIXER_SOUL.lower() or "exactly" in CI_FIXER_SOUL.lower()

    def test_agent_role_is_ci_fixer(self):
        from phalanx.agents.ci_fixer import CIFixerAgent

        assert CIFixerAgent.AGENT_ROLE == "ci_fixer"


# ── RootCauseAnalyst — JSON parsing (mocked _call_llm) ────────────────────────



class TestRootCauseAnalyst:
    """Tests for the RootCauseAnalyst LLM confirmation step (windowed API)."""

    # File content used across tests — 5 lines, error on line 1
    _FILE_LINES = [
        "import os\n",
        "import asyncio\n",
        "\n",
        "def main():\n",
        "    pass\n",
    ]

    def _make_analyst(self, llm_response: str):
        from phalanx.ci_fixer.analyst import RootCauseAnalyst

        return RootCauseAnalyst(call_llm=lambda **_: llm_response)

    def _make_parsed_log(self, tool="ruff", file="src/foo.py"):
        from phalanx.ci_fixer.log_parser import LintError, ParsedLog

        return ParsedLog(
            tool=tool,
            lint_errors=[
                LintError(file=file, line=1, col=1, code="F401", message="'os' imported but unused")
            ],
        )

    def _write_file(self, tmp_path, rel_path: str) -> None:
        """Create the test file in tmp_path so the analyst can read a window."""
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(self._FILE_LINES))

    def _patch_response(self, path: str, confidence: str = "high", **extra) -> str:
        """Build a valid windowed-patch JSON response."""
        import json as _json

        # corrected_lines = original window minus the first line (import os)
        corrected = self._FILE_LINES[1:]  # remove "import os\n"
        patch = {
            "path": path,
            "start_line": 1,
            "end_line": len(self._FILE_LINES),
            "corrected_lines": corrected,
            "reason": "removed unused import",
        }
        data = {
            "confidence": confidence,
            "root_cause": "unused import",
            "patches": [patch],
            "needs_new_test": False,
            **extra,
        }
        return _json.dumps(data)

    # ── Happy path ─────────────────────────────────────────────────────────────

    def test_valid_json_returns_fix_plan(self, tmp_path):
        self._write_file(tmp_path, "src/foo.py")
        analyst = self._make_analyst(self._patch_response("src/foo.py"))
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "high"
        assert plan.root_cause == "unused import"
        assert len(plan.patches) == 1
        assert plan.is_actionable

    def test_medium_confidence_is_actionable(self, tmp_path):
        self._write_file(tmp_path, "src/foo.py")
        analyst = self._make_analyst(self._patch_response("src/foo.py", confidence="medium"))
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "medium"
        assert plan.is_actionable

    def test_patch_delta_stored(self, tmp_path):
        """FilePatch.delta is negative when a line is removed."""
        self._write_file(tmp_path, "src/foo.py")
        analyst = self._make_analyst(self._patch_response("src/foo.py"))
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.patches[0].delta == -1   # removed 1 line (import os)

    # ── Low confidence / no patches ───────────────────────────────────────────

    def test_low_confidence_returns_empty_patches(self, tmp_path):
        self._write_file(tmp_path, "src/foo.py")
        import json as _j
        response = _j.dumps({"confidence": "low", "root_cause": "unclear",
                              "patches": [], "needs_new_test": False})
        analyst = self._make_analyst(response)
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "low"
        assert not plan.is_actionable

    def test_no_errors_returns_low_confidence(self, tmp_path):
        from phalanx.ci_fixer.analyst import RootCauseAnalyst
        from phalanx.ci_fixer.log_parser import ParsedLog

        analyst = RootCauseAnalyst(call_llm=lambda **_: "{}")
        plan = analyst.analyze(ParsedLog(tool="unknown"), tmp_path)
        assert plan.confidence == "low"

    def test_file_not_in_workspace_returns_low_confidence(self, tmp_path):
        """No file created in tmp_path → windows empty → low confidence."""
        analyst = self._make_analyst(self._patch_response("src/foo.py"))
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "low"

    # ── Guard rails ────────────────────────────────────────────────────────────

    def test_patch_for_unknown_file_rejected(self, tmp_path):
        """LLM returns a patch for a file we never sent → rejected → no actionable patches."""
        self._write_file(tmp_path, "src/foo.py")
        import json as _j
        response = _j.dumps({
            "confidence": "high",
            "root_cause": "x",
            "patches": [{
                "path": "src/invented_file.py",
                "start_line": 1, "end_line": 3,
                "corrected_lines": ["x = 1\n"],
                "reason": "invented",
            }],
            "needs_new_test": False,
        })
        analyst = self._make_analyst(response)
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        # All patches rejected → downgraded to low
        assert plan.confidence == "low"
        assert len(plan.patches) == 0

    def test_patch_for_test_file_rejected(self, tmp_path):
        """Patches targeting test files are always rejected."""
        self._write_file(tmp_path, "tests/test_foo.py")
        import json as _j
        response = _j.dumps({
            "confidence": "high",
            "root_cause": "x",
            "patches": [{
                "path": "tests/test_foo.py",
                "start_line": 1, "end_line": 3,
                "corrected_lines": ["x = 1\n"],
                "reason": "bad",
            }],
            "needs_new_test": False,
        })
        parsed = self._make_parsed_log(file="tests/test_foo.py")
        analyst = self._make_analyst(response)
        plan = analyst.analyze(parsed, tmp_path)
        assert len(plan.patches) == 0

    def test_patch_delta_too_large_rejected(self, tmp_path):
        """corrected_lines that differ by > MAX_LINE_DELTA from the window → rejected."""
        self._write_file(tmp_path, "src/foo.py")
        import json as _j
        # Window is 5 lines; returning 50 lines → delta = 45 → rejected
        big_lines = [f"line {i}\n" for i in range(50)]
        response = _j.dumps({
            "confidence": "high",
            "root_cause": "x",
            "patches": [{
                "path": "src/foo.py",
                "start_line": 1, "end_line": len(self._FILE_LINES),
                "corrected_lines": big_lines,
                "reason": "too big",
            }],
            "needs_new_test": False,
        })
        analyst = self._make_analyst(response)
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert len(plan.patches) == 0

    # ── JSON parsing edge cases ────────────────────────────────────────────────

    def test_markdown_fences_stripped(self, tmp_path):
        self._write_file(tmp_path, "src/foo.py")
        inner = self._patch_response("src/foo.py")
        response = f"```json\n{inner}\n```"
        analyst = self._make_analyst(response)
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "high"

    def test_invalid_json_returns_low_confidence(self, tmp_path):
        self._write_file(tmp_path, "src/foo.py")
        analyst = self._make_analyst("not json at all")
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "low"

    def test_exception_returns_low_confidence(self, tmp_path):
        from phalanx.ci_fixer.analyst import RootCauseAnalyst

        def bad_llm(**_):
            raise RuntimeError("API error")

        self._write_file(tmp_path, "src/foo.py")
        analyst = RootCauseAnalyst(call_llm=bad_llm)
        plan = analyst.analyze(self._make_parsed_log(), tmp_path)
        assert plan.confidence == "low"
