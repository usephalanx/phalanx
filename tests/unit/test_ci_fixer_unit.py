"""
Unit tests for CI Fixer — classifier, log_fetcher helpers, events, command parser.
No DB, no network, no Celery.
"""
from __future__ import annotations

import pytest

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
        log = (
            "FAILED tests/test_foo.py::TestA::test_1\n"
            "FAILED tests/test_foo.py::TestA::test_2\n"
        )
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
        assert result == "(no files found)"

    def test_read_files_empty_list(self, tmp_path):
        agent = self._make_agent()
        assert agent._read_files(tmp_path, []) == "(no files found)"

    def test_read_files_truncates_large_file(self, tmp_path):
        agent = self._make_agent()
        (tmp_path / "big.py").write_text("x" * 10000)
        result = agent._read_files(tmp_path, ["big.py"])
        assert "truncated" in result


# ── CI_FIXER_SOUL ──────────────────────────────────────────────────────────────

class TestCIFixerSoul:
    def test_soul_registered(self):
        from phalanx.agents.soul import get_soul, CI_FIXER_SOUL
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


# ── _generate_fix — JSON parsing (mocked _call_claude) ────────────────────────

import asyncio
from unittest.mock import patch

import pytest

class TestGenerateFix:
    def _make_agent(self):
        from phalanx.agents.ci_fixer import CIFixerAgent
        return CIFixerAgent(ci_fix_run_id="00000000-0000-0000-0000-000000000002")

    @pytest.mark.asyncio
    async def test_valid_json_parsed(self):
        agent = self._make_agent()
        import json as _json
        response = _json.dumps({
            "confidence": "high",
            "root_cause": "missing import",
            "files": [{"path": "src/foo.py", "content": "import os\n"}],
        })
        with patch.object(agent, "_call_claude", return_value=response):
            result = await agent._generate_fix("build", "log", "files", "reflection")
        assert result["confidence"] == "high"
        assert len(result["files"]) == 1

    @pytest.mark.asyncio
    async def test_low_confidence_returns_empty_files(self):
        agent = self._make_agent()
        response = '{"confidence": "low", "root_cause": "unclear", "files": []}'
        with patch.object(agent, "_call_claude", return_value=response):
            result = await agent._generate_fix("unknown", "log", "files", "")
        assert result["confidence"] == "low"
        assert result["files"] == []

    @pytest.mark.asyncio
    async def test_json_in_markdown_fences_parsed(self):
        agent = self._make_agent()
        response = '```json\n{"confidence": "high", "root_cause": "x", "files": []}\n```'
        with patch.object(agent, "_call_claude", return_value=response):
            result = await agent._generate_fix("lint", "log", "files", "")
        assert result is not None
        assert result["confidence"] == "high"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        agent = self._make_agent()
        with patch.object(agent, "_call_claude", return_value="not json at all"):
            result = await agent._generate_fix("test", "log", "files", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_claude_exception_returns_none(self):
        agent = self._make_agent()
        with patch.object(agent, "_call_claude", side_effect=Exception("API error")):
            result = await agent._generate_fix("test", "log", "files", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_medium_confidence_returned(self):
        agent = self._make_agent()
        response = '{"confidence": "medium", "root_cause": "probably this", "files": [{"path": "x.py", "content": "pass"}]}'
        with patch.object(agent, "_call_claude", return_value=response):
            result = await agent._generate_fix("type", "log", "files", "")
        assert result["confidence"] == "medium"
