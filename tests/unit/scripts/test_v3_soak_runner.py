"""Tier-1 tests for scripts/v3_soak_runner.py.

Focus: the parsing + fallback layer. The full runner loop hits prod over
SSH so it can't be unit-tested; we test the pieces we care about:

  - _parse_result_line: regression-script summary parsing
  - _query_recent_run_id_fallback: SSH-less by mocking _ssh
  - integration of the fallback into the iteration logic
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


_RUNNER_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "v3_soak_runner.py"
)


@pytest.fixture(scope="module")
def runner():
    """Import scripts/v3_soak_runner.py as a module so tests can call its
    private helpers directly."""
    spec = importlib.util.spec_from_file_location("_soak_runner", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_soak_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# _parse_result_line
# ─────────────────────────────────────────────────────────────────────────────


class TestParseResultLine:
    def test_lint_shipped_line_parses(self, runner):
        out = runner._parse_result_line(
            "lint|SHIPPED|065dc237-6d08-47e2-8cf2-3dae4ba91e2a|aaa1111|bbb2222"
        )
        assert out == {
            "cell": "lint",
            "verdict": "SHIPPED",
            "run_id": "065dc237-6d08-47e2-8cf2-3dae4ba91e2a",
            "intro_sha": "aaa1111",
            "head_sha": "bbb2222",
        }

    def test_failed_line_parses(self, runner):
        out = runner._parse_result_line(
            "coverage|FAILED|158f499c-edea-4f5a-83ed-f54f36895864|ccc|ddd"
        )
        assert out is not None
        assert out["verdict"] == "FAILED"

    def test_empty_returns_none(self, runner):
        assert runner._parse_result_line("") is None
        assert runner._parse_result_line(None) is None

    def test_too_few_parts_returns_none(self, runner):
        # Only "cell|verdict" — no run_id
        assert runner._parse_result_line("lint|SHIPPED") is None

    def test_unknown_cell_returns_none(self, runner):
        assert runner._parse_result_line("foo|SHIPPED|xxx|yyy|zzz") is None

    def test_arbitrary_log_line_returns_none(self, runner):
        # The runner's `_run_cell` walks lines bottom-up looking for a
        # matching summary; this kind of line wouldn't match.
        assert runner._parse_result_line(
            "[2026-05-04 03:54:35] some log line that isn't a summary"
        ) is None


# ─────────────────────────────────────────────────────────────────────────────
# _query_recent_run_id_fallback
# ─────────────────────────────────────────────────────────────────────────────


class TestQueryRecentRunIdFallback:
    """The fallback queries prod psql via SSH. We mock `_ssh` to test
    the function without touching the network."""

    def _start_ts(self) -> datetime:
        # An arbitrary fixed ts; the function only formats it for SQL
        return datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_returns_uuid_on_match(self, runner):
        with patch.object(
            runner, "_ssh",
            return_value="ee5fd137-1234-5678-9abc-def012345678\n",
        ):
            run_id = runner._query_recent_run_id_fallback(
                start_ts=self._start_ts(),
                cell="flake",
                ssh_key="/tmp/key",
                ssh_host="ubuntu@1.2.3.4",
                pg_container="pg",
            )
            assert run_id == "ee5fd137-1234-5678-9abc-def012345678"

    def test_returns_none_on_empty_response(self, runner):
        """No row matched (e.g., script crashed before persisting)."""
        with patch.object(runner, "_ssh", return_value=""):
            run_id = runner._query_recent_run_id_fallback(
                start_ts=self._start_ts(),
                cell="flake",
                ssh_key="/tmp/key",
                ssh_host="ubuntu@1.2.3.4",
                pg_container="pg",
            )
            assert run_id is None

    def test_returns_none_on_non_uuid_output(self, runner):
        """Defensive: if psql outputs garbage, don't fabricate a run_id."""
        with patch.object(
            runner, "_ssh",
            return_value="ERROR: relation \"runs\" does not exist\n",
        ):
            run_id = runner._query_recent_run_id_fallback(
                start_ts=self._start_ts(),
                cell="flake",
                ssh_key="/tmp/key",
                ssh_host="ubuntu@1.2.3.4",
                pg_container="pg",
            )
            assert run_id is None

    def test_query_includes_branch_filter_for_cell(self, runner):
        """The SQL must filter by the cell-branch prefix so we don't
        cross-pollinate between concurrent cells. The runner does
        round-robin so concurrency is unlikely, but be defensive."""
        seen_remote_cmds: list[str] = []

        def _capture(_key, _host, remote, timeout=None):
            seen_remote_cmds.append(remote)
            return "ee5fd137-1234-5678-9abc-def012345678\n"

        with patch.object(runner, "_ssh", side_effect=_capture):
            runner._query_recent_run_id_fallback(
                start_ts=self._start_ts(),
                cell="coverage",
                ssh_key="/tmp/key",
                ssh_host="ubuntu@1.2.3.4",
                pg_container="pg",
            )

        assert len(seen_remote_cmds) == 1
        sql = seen_remote_cmds[0]
        assert "v3-rerun/coverage-" in sql
        assert "ORDER BY r.created_at DESC LIMIT 1" in sql
        # Repo filter present
        assert "usephalanx/phalanx-ci-fixer-testbed" in sql
        # Time filter present
        assert "2026-05-04 12:00:00" in sql

    def test_query_uses_custom_repo(self, runner):
        """For non-default repos (e.g. humanize), the caller can pass a
        repo arg. Currently default-only but the kwarg is exposed."""
        seen: list[str] = []

        def _capture(_k, _h, remote, timeout=None):
            seen.append(remote)
            return ""

        with patch.object(runner, "_ssh", side_effect=_capture):
            runner._query_recent_run_id_fallback(
                start_ts=self._start_ts(),
                cell="lint",
                ssh_key="/tmp/key",
                ssh_host="ubuntu@1.2.3.4",
                pg_container="pg",
                repo="usephalanx/humanize",
            )
        assert "usephalanx/humanize" in seen[0]

    def test_only_last_line_matters_when_psql_emits_blank_lines(self, runner):
        """psql with -tA can emit a trailing blank line; helper takes
        the last non-empty line."""
        with patch.object(
            runner, "_ssh",
            return_value="ee5fd137-1234-5678-9abc-def012345678\n\n",
        ):
            run_id = runner._query_recent_run_id_fallback(
                start_ts=self._start_ts(),
                cell="flake",
                ssh_key="/tmp/key",
                ssh_host="ubuntu@1.2.3.4",
                pg_container="pg",
            )
            assert run_id == "ee5fd137-1234-5678-9abc-def012345678"
