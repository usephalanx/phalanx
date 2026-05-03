"""Tier-1 unit tests for v1.7.2.2 SRE verify scope fix.

Bug: SRE verify ran the broad workflow enumeration (e.g. `ruff check .`)
instead of TL's narrow `verify_command` (e.g. `ruff check src/calc/foo.py`).
A correct fix would land but verify would still fail because of
unrelated lint elsewhere in the testbed.

Fix: when TL's fix_spec includes `verify_command`, _execute_verify routes
to _execute_verify_narrow which runs that single command and applies the
verify_success matcher (exit_codes + stderr_excludes). Falls back to the
legacy enumeration only when TL didn't emit a verify_command.

These tests pin the routing decision + matcher logic without spinning a
real container — _exec_in_container is intercepted.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from phalanx.agents.cifix_sre import CIFixSREAgent
from phalanx.ci_fixer_v3.provisioner import ExecResult


def _make_agent() -> CIFixSREAgent:
    """Construct an agent without going through the full BaseAgent init —
    we only need the methods, not the celery/db plumbing."""
    return CIFixSREAgent(run_id="run-test", agent_id="cifix_sre", task_id="task-test")


class TestVerifyScopeRouting:
    """Pin which verify path executes based on TL fix_spec contents."""

    @pytest.fixture
    def setup_mock(self):
        return {
            "container_id": "abc123def456",
            "workspace_path": "/tmp/v3-run-test-sre",
            "mode": "setup",
        }

    def test_narrow_path_taken_when_tl_verify_command_present(self, setup_mock):
        agent = _make_agent()
        tl_fix_spec = {
            "verify_command": "ruff check src/calc/formatting.py",
            "verify_success": {"exit_codes": [0]},
        }

        async def _run():
            with (
                patch.object(agent, "_load_upstream_sre_setup", AsyncMock(return_value=setup_mock)),
                patch.object(agent, "_load_upstream_tl_fix_spec", AsyncMock(return_value=tl_fix_spec)),
                patch.object(agent, "_load_upstream_engineer_commit_sha", AsyncMock(return_value=None)),
                patch.object(agent, "_sync_sandbox_to_commit", AsyncMock(return_value={"ok": True, "method": "skipped_no_target_sha", "verified_commit_sha": None, "target_sha": None, "error_tail": None})),
                patch(
                    "phalanx.agents.cifix_sre._exec_in_container",
                    AsyncMock(return_value=ExecResult(ok=True, exit_code=0)),
                ) as exec_mock,
                patch("phalanx.agents.cifix_sre.get_db") as get_db_mock,
            ):
                get_db_mock.return_value.__aenter__.return_value = object()
                get_db_mock.return_value.__aexit__.return_value = False
                result = await agent._execute_verify(ci_context={"failing_command": "ruff check ."})
                return exec_mock, result

        exec_mock, result = asyncio.run(_run())
        # Exactly ONE exec — the narrow command, not workflow enumeration.
        assert exec_mock.call_count == 1
        # Cmd kwarg should be TL's narrow command, not the broad failing_command.
        call_kwargs = exec_mock.call_args.kwargs
        assert call_kwargs["cmd"] == "ruff check src/calc/formatting.py"
        assert result.output["verify_scope"] == "narrow_from_tl"
        assert result.output["verdict"] == "all_green"

    def test_fallback_to_broad_when_tl_verify_command_missing(self, setup_mock, tmp_path):
        agent = _make_agent()
        # Use a real tmp workspace with no .github/workflows so
        # _collect_verify_commands returns just the failing_command.
        setup = dict(setup_mock)
        setup["workspace_path"] = str(tmp_path)
        tl_fix_spec = {"root_cause": "stuff", "fix_spec": "edit foo"}  # no verify_command

        async def _run():
            with (
                patch.object(agent, "_load_upstream_sre_setup", AsyncMock(return_value=setup)),
                patch.object(agent, "_load_upstream_tl_fix_spec", AsyncMock(return_value=tl_fix_spec)),
                patch.object(agent, "_load_upstream_engineer_commit_sha", AsyncMock(return_value=None)),
                patch(
                    "phalanx.agents.cifix_sre._exec_in_container",
                    AsyncMock(return_value=ExecResult(ok=True, exit_code=0)),
                ) as exec_mock,
                patch("phalanx.agents.cifix_sre.get_db") as get_db_mock,
            ):
                get_db_mock.return_value.__aenter__.return_value = object()
                get_db_mock.return_value.__aexit__.return_value = False
                result = await agent._execute_verify(ci_context={"failing_command": "ruff check ."})
                return exec_mock, result

        exec_mock, result = asyncio.run(_run())
        # Legacy path runs each enumerated command; with empty workflow dir
        # we get exactly one (original_failing_command). No sync step on the
        # legacy path — sync only fires inside _execute_verify_narrow.
        assert exec_mock.call_count == 1
        assert exec_mock.call_args.kwargs["cmd"] == "ruff check ."
        # No narrow scope marker in legacy fallback.
        assert "verify_scope" not in result.output

    def test_fallback_when_tl_fix_spec_absent(self, setup_mock, tmp_path):
        agent = _make_agent()
        setup = dict(setup_mock)
        setup["workspace_path"] = str(tmp_path)

        async def _run():
            with (
                patch.object(agent, "_load_upstream_sre_setup", AsyncMock(return_value=setup)),
                patch.object(agent, "_load_upstream_tl_fix_spec", AsyncMock(return_value=None)),
                patch.object(agent, "_load_upstream_engineer_commit_sha", AsyncMock(return_value=None)),
                patch(
                    "phalanx.agents.cifix_sre._exec_in_container",
                    AsyncMock(return_value=ExecResult(ok=True, exit_code=0)),
                ) as exec_mock,
                patch("phalanx.agents.cifix_sre.get_db") as get_db_mock,
            ):
                get_db_mock.return_value.__aenter__.return_value = object()
                get_db_mock.return_value.__aexit__.return_value = False
                await agent._execute_verify(ci_context={"failing_command": "pytest"})
                return exec_mock

        exec_mock = asyncio.run(_run())
        # Falls through to legacy path; runs the failing_command at least.
        assert exec_mock.call_count >= 1

    def test_empty_verify_command_string_falls_back_to_broad(self, setup_mock, tmp_path):
        agent = _make_agent()
        setup = dict(setup_mock)
        setup["workspace_path"] = str(tmp_path)
        tl_fix_spec = {"verify_command": "   ", "verify_success": {"exit_codes": [0]}}

        async def _run():
            with (
                patch.object(agent, "_load_upstream_sre_setup", AsyncMock(return_value=setup)),
                patch.object(agent, "_load_upstream_tl_fix_spec", AsyncMock(return_value=tl_fix_spec)),
                patch.object(agent, "_load_upstream_engineer_commit_sha", AsyncMock(return_value=None)),
                patch(
                    "phalanx.agents.cifix_sre._exec_in_container",
                    AsyncMock(return_value=ExecResult(ok=True, exit_code=0)),
                ) as exec_mock,
                patch("phalanx.agents.cifix_sre.get_db") as get_db_mock,
            ):
                get_db_mock.return_value.__aenter__.return_value = object()
                get_db_mock.return_value.__aexit__.return_value = False
                result = await agent._execute_verify(ci_context={"failing_command": "pytest"})
                return exec_mock, result

        exec_mock, result = asyncio.run(_run())
        # Whitespace-only verify_command treated as missing.
        assert "verify_scope" not in result.output


class TestVerifySuccessMatcher:
    """The matcher must honor exit_codes + stderr_excludes."""

    @pytest.fixture
    def setup_mock(self):
        return {
            "container_id": "abc123def456",
            "workspace_path": "/tmp/v3-run-test-sre",
            "mode": "setup",
        }

    def _run_narrow(self, agent, *, exec_result, verify_success):
        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=exec_result),
            ):
                return await agent._execute_verify_narrow(
                    container_id="cid",
                    workspace_path="/tmp/ws",
                    verify_command="ruff check src/foo.py",
                    verify_success=verify_success,
                )

        return asyncio.run(_run())

    def test_exit_zero_with_default_matcher_passes(self):
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=True, exit_code=0),
            verify_success={"exit_codes": [0]},
        )
        assert result.output["verdict"] == "all_green"
        assert result.output["new_failures"] == []

    def test_exit_one_with_default_matcher_fails(self):
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=False, exit_code=1, stderr_tail="E501 line too long"),
            verify_success={"exit_codes": [0]},
        )
        assert result.output["verdict"] == "new_failures"
        assert len(result.output["new_failures"]) == 1
        assert result.output["new_failures"][0]["exit_code"] == 1

    def test_nonzero_allowed_when_in_exit_codes_list(self):
        """e.g. coverage --fail-under intentionally allows exit 0 only,
        but mypy --strict can return 0 or 1 depending on policy. The
        matcher must respect TL's declared list."""
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=False, exit_code=2, stderr_tail=""),
            verify_success={"exit_codes": [0, 2]},
        )
        assert result.output["verdict"] == "all_green"

    def test_stderr_excludes_blocks_pass_even_on_exit_zero(self):
        """Some commands return 0 even when warnings are present. TL
        can guard via stderr_excludes — e.g. {'stderr_excludes':
        ['DeprecationWarning']}."""
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(
                ok=True, exit_code=0, stderr_tail="DeprecationWarning: stuff"
            ),
            verify_success={"exit_codes": [0], "stderr_excludes": ["DeprecationWarning"]},
        )
        assert result.output["verdict"] == "new_failures"

    def test_missing_exit_codes_defaults_to_zero(self):
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=True, exit_code=0),
            verify_success={},
        )
        assert result.output["verdict"] == "all_green"

    def test_malformed_exit_codes_defaults_to_zero(self):
        """If TL emits garbage in exit_codes, fall back safely."""
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=False, exit_code=1),
            verify_success={"exit_codes": "not a list"},  # type: ignore[dict-item]
        )
        # malformed → defaults to [0] → exit_code=1 fails.
        assert result.output["verdict"] == "new_failures"

    def test_output_includes_verify_command_and_matcher(self):
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=True, exit_code=0),
            verify_success={"exit_codes": [0]},
        )
        assert result.output["verify_command"] == "ruff check src/foo.py"
        assert result.output["verify_success"]["exit_codes"] == [0]
        assert result.output["verify_scope"] == "narrow_from_tl"

    def test_jobs_list_contains_single_entry_named_tl_verify_command(self):
        agent = _make_agent()
        result = self._run_narrow(
            agent,
            exec_result=ExecResult(ok=False, exit_code=1, stderr_tail="oops"),
            verify_success={"exit_codes": [0]},
        )
        assert len(result.output["jobs"]) == 1
        assert result.output["jobs"][0]["name"] == "tl_verify_command"
        assert result.output["jobs"][0]["cmd"] == "ruff check src/foo.py"


class TestUpstreamTLFixSpecLoader:
    """The DB loader must return the latest cifix_techlead output, or None."""

    def test_returns_none_when_no_tl_task(self):
        agent = _make_agent()
        # Build a fake session whose execute() returns an empty row set.
        from unittest.mock import MagicMock

        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_tl_fix_spec(session))
        assert out is None

    def test_returns_dict_when_tl_output_present(self):
        from unittest.mock import MagicMock

        agent = _make_agent()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = (
            {"verify_command": "pytest tests/x.py::y", "confidence": 0.9},
        )
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_tl_fix_spec(session))
        assert out == {"verify_command": "pytest tests/x.py::y", "confidence": 0.9}

    def test_returns_none_when_output_is_not_a_dict(self):
        from unittest.mock import MagicMock

        agent = _make_agent()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = ("not a dict",)
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_tl_fix_spec(session))
        assert out is None


class TestSyncSandboxToCommit:
    """v1.7.2.3: SRE verify resets the sandbox workspace to the EXACT
    engineer commit_sha before running verify_command. Branch HEAD can
    move (concurrent push, force-push); the sha is the only stable ref.
    """

    def test_sync_skipped_when_no_target_sha(self):
        """When no engineer commit_sha is available (e.g. first iteration
        before engineer has run), record HEAD as verified_commit_sha and
        skip the reset. Verify still runs against whatever's in /workspace.
        """
        agent = _make_agent()

        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=ExecResult(
                    ok=True, exit_code=0, stdout_tail="abc1234567890\n"
                )),
            ):
                return await agent._sync_sandbox_to_commit(
                    container_id="cid",
                    target_sha=None,
                    branch="main",
                )

        info = asyncio.run(_run())
        assert info["method"] == "skipped_no_target_sha"
        assert info["target_sha"] is None
        assert info["verified_commit_sha"] == "abc1234567890"

    def test_sync_succeeds_when_fetch_reset_lands_at_target(self):
        agent = _make_agent()
        target = "deadbeefcafe1234"

        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=ExecResult(
                    ok=True, exit_code=0, stdout_tail=f"{target}\n",
                )),
            ):
                return await agent._sync_sandbox_to_commit(
                    container_id="cid",
                    target_sha=target,
                    branch="fix/foo",
                )

        info = asyncio.run(_run())
        assert info["ok"] is True
        assert info["verified_commit_sha"] == target
        assert info["target_sha"] == target
        assert info["method"] == "git_fetch_reset_hard"

    def test_sync_marks_mismatch_when_head_drifts(self):
        """If something resets HEAD to a different sha than what we asked
        for (network glitch, race), `ok` must be False so the commander
        gate can reject the verify outcome as untrusted.
        """
        agent = _make_agent()

        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=ExecResult(
                    ok=True, exit_code=0, stdout_tail="ffffffffffffffff\n",
                )),
            ):
                return await agent._sync_sandbox_to_commit(
                    container_id="cid",
                    target_sha="deadbeefcafe1234",
                    branch="main",
                )

        info = asyncio.run(_run())
        assert info["ok"] is False
        assert info["verified_commit_sha"] == "ffffffffffffffff"
        assert info["target_sha"] == "deadbeefcafe1234"

    def test_sync_records_failure_when_git_fails(self):
        agent = _make_agent()

        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=ExecResult(
                    ok=False, exit_code=128, stderr_tail="fatal: bad object",
                )),
            ):
                return await agent._sync_sandbox_to_commit(
                    container_id="cid",
                    target_sha="deadbeefcafe1234",
                    branch="main",
                )

        info = asyncio.run(_run())
        assert info["ok"] is False
        assert info["method"] == "fetch_reset_failed"
        assert "fatal: bad object" in (info["error_tail"] or "")

    def test_verify_output_includes_sha_provenance(self):
        """The full _execute_verify_narrow output must surface
        engineer_commit_sha + verified_commit_sha so commander can
        cross-check them in its iteration gate.
        """
        agent = _make_agent()
        target = "1234567890abcdef"

        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=ExecResult(
                    ok=True, exit_code=0, stdout_tail=f"{target}\n",
                )),
            ):
                return await agent._execute_verify_narrow(
                    container_id="cid",
                    workspace_path="/tmp/ws",
                    verify_command="ruff check src/foo.py",
                    verify_success={"exit_codes": [0]},
                    engineer_commit_sha=target,
                    ci_context={"branch": "fix/foo"},
                )

        result = asyncio.run(_run())
        assert result.output["engineer_commit_sha"] == target
        assert result.output["verified_commit_sha"] == target
        assert result.output["sandbox_sync"]["ok"] is True
        assert result.output["sandbox_sync"]["method"] == "git_fetch_reset_hard"

    def test_failure_payload_includes_stdout_tail_v1723(self):
        """Fix 1: stdout_tail must surface in new_failures so TL replan
        iterations have the actual ruff/pytest violation text (not just
        an empty stderr). The lint cell run pre-v1.7.2.3 was blind here.
        """
        agent = _make_agent()
        violation_text = "src/foo.py:10:1: E501 Line too long (130 > 100)"

        async def _run():
            with patch(
                "phalanx.agents.cifix_sre._exec_in_container",
                AsyncMock(return_value=ExecResult(
                    ok=False, exit_code=1,
                    stderr_tail="",
                    stdout_tail=violation_text,
                )),
            ):
                return await agent._execute_verify_narrow(
                    container_id="cid",
                    workspace_path="/tmp/ws",
                    verify_command="ruff check src/foo.py",
                    verify_success={"exit_codes": [0]},
                    engineer_commit_sha=None,
                    ci_context={"branch": "fix/foo"},
                )

        result = asyncio.run(_run())
        assert result.output["verdict"] == "new_failures"
        assert result.output["jobs"][0]["stdout_tail"] == violation_text
        assert result.output["new_failures"][0]["stdout_tail"] == violation_text


class TestUpstreamEngineerCommitShaLoader:
    """v1.7.2.3: loader for the latest engineer commit_sha."""

    def test_returns_sha_when_engineer_completed(self):
        from unittest.mock import MagicMock
        agent = _make_agent()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = (
            {"commit_sha": "abc1234567890def", "v17_path": True},
        )
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_engineer_commit_sha(session))
        assert out == "abc1234567890def"

    def test_returns_none_when_no_engineer_task(self):
        from unittest.mock import MagicMock
        agent = _make_agent()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_engineer_commit_sha(session))
        assert out is None

    def test_returns_none_when_commit_sha_missing(self):
        """Engineer aborted on low-confidence — committed:false. No sha."""
        from unittest.mock import MagicMock
        agent = _make_agent()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = (
            {"committed": False, "skipped_reason": "low_confidence"},
        )
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_engineer_commit_sha(session))
        assert out is None

    def test_returns_none_when_sha_is_blank(self):
        from unittest.mock import MagicMock
        agent = _make_agent()
        session = MagicMock()
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = ({"commit_sha": "  "},)
        session.execute = AsyncMock(return_value=result_mock)
        out = asyncio.run(agent._load_upstream_engineer_commit_sha(session))
        assert out is None
