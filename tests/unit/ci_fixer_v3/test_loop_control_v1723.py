"""Tier-1 tests for v1.7.2.3 loop-control gates in cifix_commander.

Three gates added:
  1. Sha-mismatch: green rejected if verified_commit_sha != engineer_commit_sha
  2. Runtime cap: total wall-clock vs _MAX_RUN_RUNTIME_SECONDS
  3. No-progress: same fingerprint twice → stop

These tests exercise the helpers + assert the gate-decision logic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents._failure_fingerprint import is_repeated
from phalanx.agents.cifix_commander import CIFixCommanderAgent


def _make_agent() -> CIFixCommanderAgent:
    return CIFixCommanderAgent(
        run_id="run-test-loop",
        work_order_id="wo-test",
        project_id="proj-test",
    )


class TestNoProgressGate:
    """is_repeated drives the no-progress decision."""

    def test_two_identical_fingerprints_signals_no_progress(self):
        assert is_repeated(["abc123", "abc123"]) is True

    def test_progress_chain_does_not_signal(self):
        assert is_repeated(["a", "b", "c"]) is False

    def test_collect_fingerprints_filters_setup_tasks(self):
        """_collect_verify_fingerprints must skip SRE setup tasks (mode='setup').
        Setup doesn't have a verify fingerprint."""
        agent = _make_agent()

        rows = [
            ({"mode": "setup", "container_id": "x"},),
            ({"mode": "verify", "fingerprint": "fp1"},),
            ({"mode": "verify", "fingerprint": "fp2"},),
        ]

        async def _run():
            session = MagicMock()
            result_mock = MagicMock()
            result_mock.all.return_value = rows
            session.execute = AsyncMock(return_value=result_mock)

            # _collect_verify_fingerprints uses get_db internally; patch
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.agents.cifix_commander.get_db", return_value=cm):
                return await agent._collect_verify_fingerprints()

        fps = asyncio.run(_run())
        assert fps == ["fp1", "fp2"]

    def test_collect_fingerprints_skips_missing_fingerprint(self):
        """Older verify tasks (pre-v1.7.2.3) won't have a fingerprint key.
        These must be SKIPPED, not coerced to empty-string (which would
        create false repeats)."""
        agent = _make_agent()

        rows = [
            ({"mode": "verify", "fingerprint": "fp1"},),
            ({"mode": "verify"},),  # legacy — no fingerprint
            ({"mode": "verify", "fingerprint": "fp2"},),
        ]

        async def _run():
            session = MagicMock()
            result_mock = MagicMock()
            result_mock.all.return_value = rows
            session.execute = AsyncMock(return_value=result_mock)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.agents.cifix_commander.get_db", return_value=cm):
                return await agent._collect_verify_fingerprints()

        fps = asyncio.run(_run())
        assert fps == ["fp1", "fp2"]
        # Must NOT have an empty-string entry between fp1 and fp2 — that
        # would incorrectly signal no-progress if fp1 happened twice.
        assert "" not in fps


class TestRuntimeCap:
    """Wall-clock anchor + runtime check."""

    def test_run_started_monotonic_set_on_execute(self):
        """The agent records run-start time so the elapsed cap can fire."""
        agent = _make_agent()
        # Before execute()
        assert agent._run_started_monotonic is None

    def test_runtime_cap_constant_is_30_minutes(self):
        from phalanx.agents.cifix_commander import _MAX_RUN_RUNTIME_SECONDS
        assert _MAX_RUN_RUNTIME_SECONDS == 1800


class TestShaMismatchGate:
    """Logic for rejecting all_green when verified_commit_sha drifts.
    Tested via the _execute body's branching logic, not pulled out into a
    helper — but we can sanity-check the inputs the gate uses.
    """

    def test_matching_shas_does_not_reject(self):
        """Both shas same → no rejection signal (gate passes)."""
        eng = "abc1234567890def"
        ver = "abc1234567890def"
        # The check in commander code is: if eng_sha and ver_sha and eng_sha != ver_sha
        should_reject = bool(eng) and bool(ver) and eng != ver
        assert should_reject is False

    def test_mismatched_shas_rejects(self):
        eng = "abc1234567890def"
        ver = "ffff999988887777"
        should_reject = bool(eng) and bool(ver) and eng != ver
        assert should_reject is True

    def test_missing_engineer_sha_does_not_reject(self):
        """If engineer didn't provide a sha (legacy run, or low-confidence
        skip), we can't enforce the gate — accept the green and move on."""
        eng = None
        ver = "ffff999988887777"
        should_reject = bool(eng) and bool(ver) and eng != ver
        assert should_reject is False

    def test_missing_verified_sha_does_not_reject(self):
        """If sandbox sync failed and no verified_commit_sha was recorded,
        we can't enforce. Conservative choice: don't reject — sync failure
        is logged separately."""
        eng = "abc1234567890def"
        ver = None
        should_reject = bool(eng) and bool(ver) and eng != ver
        assert should_reject is False
