"""
Phase 4 unit tests for CI Fixer:
  - version_parity: check_version_parity all cases
  - should_auto_merge: all decision combinations
  - format_parity_notice: output format
  - CIIntegration.auto_merge/min_success_count column existence
"""

from __future__ import annotations

from phalanx.ci_fixer.version_parity import (
    VersionParityResult,
    check_version_parity,
    format_parity_notice,
    should_auto_merge,
)

# ── check_version_parity ───────────────────────────────────────────────────────


class TestCheckVersionParity:
    def test_same_version_ok(self):
        r = check_version_parity("ruff 0.4.1", "ruff 0.4.1")
        assert r.ok is True

    def test_patch_diff_ok(self):
        """Patch version difference is acceptable."""
        r = check_version_parity("ruff 0.4.2", "ruff 0.4.1")
        assert r.ok is True

    def test_minor_diff_not_ok(self):
        """Minor version difference → parity fails."""
        r = check_version_parity("ruff 0.5.0", "ruff 0.4.1")
        assert r.ok is False
        assert "mismatch" in r.reason.lower()

    def test_major_diff_not_ok(self):
        """Major version difference → parity fails."""
        r = check_version_parity("mypy 2.0.0", "mypy 1.10.0")
        assert r.ok is False

    def test_empty_local_ok(self):
        """Empty local version → can't compare, assume OK."""
        r = check_version_parity("", "ruff 0.4.1")
        assert r.ok is True

    def test_empty_failure_ok(self):
        """Empty failure version → can't compare, assume OK."""
        r = check_version_parity("ruff 0.4.1", "")
        assert r.ok is True

    def test_both_empty_ok(self):
        r = check_version_parity("", "")
        assert r.ok is True

    def test_different_tools_ok(self):
        """Different tools → not comparable → assume OK."""
        r = check_version_parity("ruff 0.4.1", "mypy 1.0.0")
        assert r.ok is True

    def test_unparseable_version_ok(self):
        """Version strings without semver → can't parse → assume OK."""
        r = check_version_parity("ruff dev-build", "ruff 0.4.1")
        assert r.ok is True

    def test_result_has_version_strings(self):
        r = check_version_parity("ruff 0.4.1", "ruff 0.5.0")
        assert r.local_version == "ruff 0.4.1"
        assert r.failure_version == "ruff 0.5.0"

    def test_version_in_reason(self):
        r = check_version_parity("ruff 0.4.2", "ruff 0.4.1")
        assert r.reason  # non-empty reason
        assert r.ok is True


# ── should_auto_merge ──────────────────────────────────────────────────────────


class TestShouldAutoMerge:
    def test_all_conditions_met_true(self):
        assert should_auto_merge(
            integration_auto_merge=True,
            fingerprint_success_count=5,
            min_success_count=3,
            parity_ok=True,
        )

    def test_auto_merge_disabled_false(self):
        assert not should_auto_merge(
            integration_auto_merge=False,
            fingerprint_success_count=5,
            min_success_count=3,
            parity_ok=True,
        )

    def test_insufficient_history_false(self):
        """success_count < min_success_count → no auto-merge."""
        assert not should_auto_merge(
            integration_auto_merge=True,
            fingerprint_success_count=2,
            min_success_count=3,
            parity_ok=True,
        )

    def test_parity_mismatch_false(self):
        """Tool version parity failure → no auto-merge."""
        assert not should_auto_merge(
            integration_auto_merge=True,
            fingerprint_success_count=5,
            min_success_count=3,
            parity_ok=False,
        )

    def test_exact_min_count_true(self):
        """Exactly at min_success_count threshold → auto-merge allowed."""
        assert should_auto_merge(
            integration_auto_merge=True,
            fingerprint_success_count=3,
            min_success_count=3,
            parity_ok=True,
        )

    def test_zero_success_count_false(self):
        assert not should_auto_merge(
            integration_auto_merge=True,
            fingerprint_success_count=0,
            min_success_count=3,
            parity_ok=True,
        )


# ── format_parity_notice ───────────────────────────────────────────────────────


class TestFormatParityNotice:
    def test_ok_notice(self):
        r = VersionParityResult(
            ok=True,
            local_version="ruff 0.4.1",
            failure_version="ruff 0.4.1",
            reason="versions match",
        )
        notice = format_parity_notice(r)
        assert "✅" in notice
        assert "ruff 0.4.1" in notice

    def test_ok_notice_no_version(self):
        r = VersionParityResult(ok=True, local_version="", failure_version="", reason="skipped")
        notice = format_parity_notice(r)
        assert "✅" in notice

    def test_mismatch_notice(self):
        r = VersionParityResult(
            ok=False,
            local_version="ruff 0.5.0",
            failure_version="ruff 0.4.1",
            reason="minor version mismatch",
        )
        notice = format_parity_notice(r)
        assert "⚠️" in notice
        assert "ruff 0.5.0" in notice
        assert "ruff 0.4.1" in notice


# ── ORM model column existence ─────────────────────────────────────────────────


def test_ci_integration_auto_merge_column_exists():
    """Phase 4 columns exist on CIIntegration model."""
    from phalanx.db.models import CIIntegration

    # Verify the mapped columns exist by inspecting the class
    assert hasattr(CIIntegration, "auto_merge")
    assert hasattr(CIIntegration, "min_success_count")


def test_ci_fix_run_parity_column_exists():
    """Phase 4 column exists on CIFixRun model."""
    from phalanx.db.models import CIFixRun

    assert hasattr(CIFixRun, "tool_version_parity_ok")
