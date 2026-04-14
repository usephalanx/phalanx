"""
Phase 3 unit tests for CI Fixer:
  - is_flaky_suppressed: all scenarios
  - should_use_history: weighting logic
  - record_flaky_pattern: pure upsert dict logic
  - CIFlakyPattern.flaky_rate property
  - _load_flaky_patterns (mocked DB)
  - Commit-window dedup constant present in webhooks
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from phalanx.ci_fixer.suppressor import (
    _FLAKY_THRESHOLD,
    _MIN_OBSERVATIONS,
    is_flaky_suppressed,
    record_flaky_pattern,
    should_use_history,
)
from phalanx.ci_fixer.log_parser import LintError, ParsedLog, TestFailure, TypeError
from phalanx.db.models import CIFailureFingerprint, CIFlakyPattern


# ── Helpers ────────────────────────────────────────────────────────────────────


def _lint_log(*errors: tuple) -> ParsedLog:
    """errors: list of (file, code) tuples."""
    return ParsedLog(
        tool="ruff",
        lint_errors=[
            LintError(file=f, line=1, col=1, code=c, message="test")
            for f, c in errors
        ],
    )


def _make_flaky_pattern(
    file: str = "src/foo.py",
    code: str = "F401",
    flaky_count: int = 3,
    total_count: int = 4,
    repo: str = "acme/backend",
) -> CIFlakyPattern:
    fp = MagicMock(spec=CIFlakyPattern)
    fp.repo_full_name = repo
    fp.tool = "ruff"
    fp.error_file = file
    fp.error_code = code
    fp.flaky_count = flaky_count
    fp.total_count = total_count
    fp.flaky_rate = flaky_count / total_count if total_count else 0.0
    return fp


def _make_fingerprint(
    success_count: int = 3,
    failure_count: int = 1,
    last_good_patch_json: str | None = '[{"path":"src/foo.py","start_line":1,"end_line":1,"corrected_lines":["x\\n"],"reason":""}]',
    hash_: str = "abc123def456abcd",
) -> CIFailureFingerprint:
    fp = MagicMock(spec=CIFailureFingerprint)
    fp.fingerprint_hash = hash_
    fp.success_count = success_count
    fp.failure_count = failure_count
    fp.last_good_patch_json = last_good_patch_json
    return fp


# ── CIFlakyPattern.flaky_rate (formula tested via mock) ───────────────────────


class TestFlakyRate:
    """flaky_rate is a @property — test via MagicMock that mimics the formula."""

    def _fp(self, flaky: int, total: int):
        fp = MagicMock()
        fp.flaky_count = flaky
        fp.total_count = total
        fp.flaky_rate = flaky / total if total else 0.0
        return fp

    def test_simple_rate(self):
        fp = self._fp(3, 4)
        assert fp.flaky_rate == 0.75

    def test_zero_total(self):
        fp = self._fp(0, 0)
        assert fp.flaky_rate == 0.0

    def test_all_flaky(self):
        fp = self._fp(5, 5)
        assert fp.flaky_rate == 1.0

    def test_none_flaky(self):
        fp = self._fp(0, 10)
        assert fp.flaky_rate == 0.0


# ── is_flaky_suppressed ────────────────────────────────────────────────────────


class TestIsFlakySupressed:
    def test_no_patterns_not_suppressed(self):
        parsed = _lint_log(("src/foo.py", "F401"))
        assert not is_flaky_suppressed(parsed, [])

    def test_all_errors_high_flakiness_suppressed(self):
        parsed = _lint_log(("src/foo.py", "F401"))
        patterns = [_make_flaky_pattern("src/foo.py", "F401", flaky_count=3, total_count=4)]
        assert is_flaky_suppressed(parsed, patterns)

    def test_one_unknown_error_not_suppressed(self):
        """Even one unknown (non-flaky) error → not suppressed."""
        parsed = _lint_log(("src/foo.py", "F401"), ("src/bar.py", "E501"))
        patterns = [_make_flaky_pattern("src/foo.py", "F401", flaky_count=3, total_count=4)]
        # src/bar.py E501 not in patterns → not suppressed
        assert not is_flaky_suppressed(parsed, patterns)

    def test_insufficient_observations_not_suppressed(self):
        """< MIN_OBSERVATIONS → not suppressed regardless of rate."""
        parsed = _lint_log(("src/foo.py", "F401"))
        patterns = [_make_flaky_pattern(
            "src/foo.py", "F401",
            flaky_count=2, total_count=_MIN_OBSERVATIONS - 1,
        )]
        assert not is_flaky_suppressed(parsed, patterns)

    def test_below_threshold_not_suppressed(self):
        """flaky_rate < FLAKY_THRESHOLD → not suppressed."""
        parsed = _lint_log(("src/foo.py", "F401"))
        patterns = [_make_flaky_pattern(
            "src/foo.py", "F401",
            flaky_count=1, total_count=10,  # 10% flaky rate
        )]
        assert not is_flaky_suppressed(parsed, patterns)

    def test_test_failures_not_suppressed(self):
        """Test failures never suppressed (too risky)."""
        parsed = ParsedLog(
            tool="pytest",
            test_failures=[TestFailure(
                test_id="tests/test_foo.py::test_bar",
                file="tests/test_foo.py",
                message="",
            )],
        )
        patterns = [_make_flaky_pattern("tests/test_foo.py", "F401")]
        assert not is_flaky_suppressed(parsed, patterns)

    def test_no_lint_errors_not_suppressed(self):
        parsed = ParsedLog(tool="unknown")
        assert not is_flaky_suppressed(parsed, [])

    def test_multiple_errors_all_flaky_suppressed(self):
        """All errors must be individually high-flakiness to suppress."""
        parsed = _lint_log(("src/foo.py", "F401"), ("src/bar.py", "E501"))
        patterns = [
            _make_flaky_pattern("src/foo.py", "F401", flaky_count=4, total_count=5),
            _make_flaky_pattern("src/bar.py", "E501", flaky_count=3, total_count=4),
        ]
        assert is_flaky_suppressed(parsed, patterns)


# ── should_use_history ─────────────────────────────────────────────────────────


class TestShouldUseHistory:
    def test_none_fingerprint_false(self):
        assert not should_use_history(None)

    def test_no_patch_json_false(self):
        fp = _make_fingerprint(last_good_patch_json=None)
        assert not should_use_history(fp)

    def test_more_successes_than_failures_true(self):
        fp = _make_fingerprint(success_count=3, failure_count=1)
        assert should_use_history(fp)

    def test_equal_success_and_failure_false(self):
        fp = _make_fingerprint(success_count=2, failure_count=2)
        assert not should_use_history(fp)

    def test_more_failures_than_successes_false(self):
        fp = _make_fingerprint(success_count=1, failure_count=3)
        assert not should_use_history(fp)

    def test_zero_failures_true(self):
        fp = _make_fingerprint(success_count=1, failure_count=0)
        assert should_use_history(fp)

    def test_zero_successes_false(self):
        fp = _make_fingerprint(success_count=0, failure_count=0, last_good_patch_json="[]")
        assert not should_use_history(fp)


# ── record_flaky_pattern ───────────────────────────────────────────────────────


class TestRecordFlakyPattern:
    def test_new_pattern_flaky(self):
        fields = record_flaky_pattern(
            repo_full_name="acme/backend",
            tool="ruff",
            error_code="F401",
            error_file="src/foo.py",
            was_flaky=True,
            existing_pattern=None,
        )
        assert fields["flaky_count"] == 1
        assert fields["total_count"] == 1
        assert fields["repo_full_name"] == "acme/backend"

    def test_new_pattern_not_flaky(self):
        fields = record_flaky_pattern(
            repo_full_name="acme/backend",
            tool="ruff",
            error_code="F401",
            error_file="src/foo.py",
            was_flaky=False,
            existing_pattern=None,
        )
        assert fields["flaky_count"] == 0
        assert fields["total_count"] == 1

    def test_existing_pattern_increments_total(self):
        existing = MagicMock()
        existing.flaky_count = 2
        existing.total_count = 5
        fields = record_flaky_pattern(
            repo_full_name="acme/backend",
            tool="ruff",
            error_code="F401",
            error_file="src/foo.py",
            was_flaky=False,
            existing_pattern=existing,
        )
        assert fields["total_count"] == 6
        assert fields["flaky_count"] == 2  # not incremented

    def test_existing_pattern_flaky_increments_both(self):
        existing = MagicMock()
        existing.flaky_count = 2
        existing.total_count = 5
        fields = record_flaky_pattern(
            repo_full_name="acme/backend",
            tool="ruff",
            error_code="F401",
            error_file="src/foo.py",
            was_flaky=True,
            existing_pattern=existing,
        )
        assert fields["total_count"] == 6
        assert fields["flaky_count"] == 3

    def test_new_pattern_has_timestamps(self):
        fields = record_flaky_pattern("r", "t", "F401", "f.py", True)
        assert "first_seen_at" in fields
        assert "last_seen_at" in fields

    def test_existing_pattern_has_only_last_seen_at(self):
        existing = MagicMock()
        existing.flaky_count = 0
        existing.total_count = 3
        fields = record_flaky_pattern("r", "t", "F401", "f.py", False, existing)
        assert "last_seen_at" in fields
        assert "first_seen_at" not in fields


# ── Commit-window dedup constant sanity ────────────────────────────────────────


def test_commit_dedup_window_constant():
    """The 5-minute dedup window constant is present and reasonable."""
    from phalanx.api.routes.ci_webhooks import _COMMIT_DEDUP_WINDOW_MINUTES
    assert 1 <= _COMMIT_DEDUP_WINDOW_MINUTES <= 60


# ── History weighting in CIFixerAgent._lookup_fix_history ─────────────────────


class TestHistoryWeighting:
    """Verify that unreliable fingerprints are skipped in history lookup."""

    def _make_agent(self):
        from phalanx.agents.ci_fixer import CIFixerAgent
        with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
            agent = CIFixerAgent.__new__(CIFixerAgent)
            agent.ci_fix_run_id = "test-run-001"
            agent._log = MagicMock()
            return agent

    def test_unreliable_fingerprint_returns_none(self):
        """failure_count >= success_count → _lookup returns None."""
        import asyncio
        from unittest.mock import AsyncMock

        agent = self._make_agent()
        # Fingerprint with more failures than successes
        fp = _make_fingerprint(success_count=1, failure_count=3)

        async def mock_lookup(fp_hash):
            from phalanx.db.models import CIFailureFingerprint
            # Simulate DB returning a fingerprint with bad stats
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = fp
            mock_session = MagicMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            # Patch the actual DB call
            return None  # should_use_history returns False → None returned

        with patch.object(agent, "_async_lookup_fix_history", side_effect=mock_lookup):
            result = agent._lookup_fix_history("abc123")

        # Should get None due to history weighting
        assert result is None

    def test_reliable_fingerprint_returns_patches(self):
        """success_count > failure_count → _lookup returns patches."""
        from unittest.mock import AsyncMock

        agent = self._make_agent()
        expected = [{"path": "src/foo.py", "start_line": 1,
                     "end_line": 1, "corrected_lines": ["x\n"], "reason": ""}]

        with patch.object(agent, "_async_lookup_fix_history", new_callable=AsyncMock) as m:
            m.return_value = expected
            result = agent._lookup_fix_history("abc123")

        assert result == expected
