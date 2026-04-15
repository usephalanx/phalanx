"""
Phase 5 unit tests:
  - is_promotion_eligible: thresholds
  - ProactiveFinding.to_dict
  - format_proactive_comment: output format
  - should_post_proactive_comment: severity filtering
  - CIPatternRegistry / CIProactiveScan model columns
"""

from __future__ import annotations

from phalanx.ci_fixer.pattern_promoter import (
    MIN_GLOBAL_SUCCESS_COUNT,
    MIN_REPOS_FOR_PROMOTION,
    is_promotion_eligible,
)
from phalanx.ci_fixer.proactive_scanner import (
    ProactiveFinding,
    format_proactive_comment,
    should_post_proactive_comment,
)

# ── is_promotion_eligible ──────────────────────────────────────────────────────


class TestIsPromotionEligible:
    def test_enough_repos(self):
        assert is_promotion_eligible(MIN_REPOS_FOR_PROMOTION, 1)

    def test_not_enough_repos_not_enough_global(self):
        assert not is_promotion_eligible(1, MIN_GLOBAL_SUCCESS_COUNT - 1)

    def test_high_global_count_single_repo(self):
        assert is_promotion_eligible(1, MIN_GLOBAL_SUCCESS_COUNT)

    def test_zero_everything_false(self):
        assert not is_promotion_eligible(0, 0)

    def test_exactly_at_threshold_repos(self):
        assert is_promotion_eligible(MIN_REPOS_FOR_PROMOTION, 0)

    def test_just_below_threshold_global(self):
        assert not is_promotion_eligible(1, MIN_GLOBAL_SUCCESS_COUNT - 1)


# ── ProactiveFinding ───────────────────────────────────────────────────────────


class TestProactiveFinding:
    def _finding(self, severity: str = "warning") -> ProactiveFinding:
        return ProactiveFinding(
            fingerprint_hash="abc123",
            tool="ruff",
            description="unused import pattern",
            severity=severity,
            affected_files=["src/foo.py", "src/bar.py"],
        )

    def test_to_dict(self):
        f = self._finding()
        d = f.to_dict()
        assert d["fingerprint_hash"] == "abc123"
        assert d["tool"] == "ruff"
        assert d["severity"] == "warning"
        assert "src/foo.py" in d["affected_files"]

    def test_severity_warning(self):
        f = self._finding("warning")
        assert f.severity == "warning"

    def test_severity_info(self):
        f = self._finding("info")
        assert f.severity == "info"


# ── format_proactive_comment ───────────────────────────────────────────────────


class TestFormatProactiveComment:
    def _warnings(self, n: int = 2) -> list[ProactiveFinding]:
        return [
            ProactiveFinding(
                fingerprint_hash=f"fp{i}",
                tool="ruff",
                description=f"Pattern {i}: unused import",
                severity="warning",
                affected_files=[f"src/file{i}.py"],
            )
            for i in range(n)
        ]

    def test_empty_findings_empty_string(self):
        assert format_proactive_comment([], 42) == ""

    def test_warning_findings_include_pr_context(self):
        comment = format_proactive_comment(self._warnings(1), 42)
        assert "Phalanx" in comment
        assert "1 pattern" in comment

    def test_multiple_warnings_shown(self):
        comment = format_proactive_comment(self._warnings(3), 42)
        assert "3 pattern" in comment

    def test_table_headers_present(self):
        comment = format_proactive_comment(self._warnings(1), 42)
        assert "Pattern" in comment
        assert "Tool" in comment
        assert "Severity" in comment

    def test_tool_name_in_comment(self):
        comment = format_proactive_comment(self._warnings(1), 42)
        assert "ruff" in comment

    def test_info_findings_different_header(self):
        findings = [
            ProactiveFinding("fp1", "ruff", "info pattern", "info", ["f.py"])
        ]
        comment = format_proactive_comment(findings, 42)
        assert "informational" in comment.lower() or "info" in comment.lower()

    def test_max_10_findings_shown(self):
        """Comment should cap at 10 findings."""
        findings = [
            ProactiveFinding(f"fp{i}", "ruff", f"Pattern {i}", "warning", [f"f{i}.py"])
            for i in range(15)
        ]
        comment = format_proactive_comment(findings, 42)
        # Should not show all 15 — truncated in table
        assert "f14.py" not in comment or comment.count("Pattern") <= 10

    def test_footer_present(self):
        comment = format_proactive_comment(self._warnings(1), 42)
        assert "does not block" in comment.lower() or "not block" in comment.lower()


# ── should_post_proactive_comment ──────────────────────────────────────────────


class TestShouldPostProactiveComment:
    def test_no_findings_false(self):
        assert not should_post_proactive_comment([])

    def test_only_info_findings_false(self):
        findings = [
            ProactiveFinding("fp1", "ruff", "info", "info", ["f.py"])
        ]
        assert not should_post_proactive_comment(findings)

    def test_warning_finding_true(self):
        findings = [
            ProactiveFinding("fp1", "ruff", "warning pattern", "warning", ["f.py"])
        ]
        assert should_post_proactive_comment(findings)

    def test_mixed_info_and_warning_true(self):
        findings = [
            ProactiveFinding("fp1", "ruff", "info", "info", ["f.py"]),
            ProactiveFinding("fp2", "mypy", "warn", "warning", ["g.py"]),
        ]
        assert should_post_proactive_comment(findings)


# ── Model column existence ─────────────────────────────────────────────────────


def test_pattern_registry_columns():
    from phalanx.db.models import CIPatternRegistry
    assert hasattr(CIPatternRegistry, "fingerprint_hash")
    assert hasattr(CIPatternRegistry, "tool")
    assert hasattr(CIPatternRegistry, "repo_count")
    assert hasattr(CIPatternRegistry, "total_success_count")
    assert hasattr(CIPatternRegistry, "patch_template_json")


def test_proactive_scan_columns():
    from phalanx.db.models import CIProactiveScan
    assert hasattr(CIProactiveScan, "repo_full_name")
    assert hasattr(CIProactiveScan, "pr_number")
    assert hasattr(CIProactiveScan, "findings_json")
    assert hasattr(CIProactiveScan, "comment_posted")
