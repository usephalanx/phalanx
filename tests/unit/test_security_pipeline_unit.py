"""
Unit tests for phalanx/guardrails/security_pipeline.py.

Tests the data types and individual scanner functions with mocked subprocess calls.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from phalanx.guardrails.security_pipeline import (
    _SEVERITY_RANK,
    ScanFinding,
    ScanResult,
    ScanSeverity,
    SecurityScanResult,
    run_bandit,
)

# ── ScanSeverity ──────────────────────────────────────────────────────────────


class TestScanSeverity:
    def test_severity_values(self):
        assert ScanSeverity.NONE == "none"
        assert ScanSeverity.LOW == "low"
        assert ScanSeverity.MEDIUM == "medium"
        assert ScanSeverity.HIGH == "high"
        assert ScanSeverity.CRITICAL == "critical"

    def test_severity_rank_order(self):
        assert _SEVERITY_RANK[ScanSeverity.NONE] < _SEVERITY_RANK[ScanSeverity.LOW]
        assert _SEVERITY_RANK[ScanSeverity.LOW] < _SEVERITY_RANK[ScanSeverity.MEDIUM]
        assert _SEVERITY_RANK[ScanSeverity.MEDIUM] < _SEVERITY_RANK[ScanSeverity.HIGH]
        assert _SEVERITY_RANK[ScanSeverity.HIGH] < _SEVERITY_RANK[ScanSeverity.CRITICAL]


# ── ScanFinding ───────────────────────────────────────────────────────────────


class TestScanFinding:
    def test_as_dict_contains_all_fields(self):
        finding = ScanFinding(
            tool="bandit",
            severity=ScanSeverity.HIGH,
            title="B101",
            location="phalanx/auth.py:42",
            detail="Use of assert statement",
            cve=None,
        )
        d = finding.as_dict()
        assert d["tool"] == "bandit"
        assert d["severity"] == ScanSeverity.HIGH
        assert d["title"] == "B101"
        assert d["location"] == "phalanx/auth.py:42"
        assert d["cve"] is None

    def test_finding_with_cve(self):
        finding = ScanFinding(
            tool="pip-audit",
            severity=ScanSeverity.CRITICAL,
            title="CVE-2024-1234",
            location="requirements.txt",
            detail="Vulnerable package",
            cve="CVE-2024-1234",
        )
        d = finding.as_dict()
        assert d["cve"] == "CVE-2024-1234"


# ── ScanResult ────────────────────────────────────────────────────────────────


class TestScanResult:
    def test_passed_scan_as_dict(self):
        result = ScanResult(tool="bandit", passed=True, max_severity=ScanSeverity.NONE)
        d = result.as_dict()
        assert d["tool"] == "bandit"
        assert d["passed"] is True
        assert d["findings"] == []
        assert d["error"] is None

    def test_failed_scan_with_findings(self):
        finding = ScanFinding(
            tool="bandit",
            severity=ScanSeverity.HIGH,
            title="B105",
            location="app.py:10",
            detail="Hardcoded password",
        )
        result = ScanResult(
            tool="bandit",
            passed=False,
            max_severity=ScanSeverity.HIGH,
            findings=[finding],
        )
        d = result.as_dict()
        assert d["passed"] is False
        assert len(d["findings"]) == 1

    def test_scan_result_with_error(self):
        result = ScanResult(tool="bandit", passed=False, error="bandit not installed")
        d = result.as_dict()
        assert d["error"] == "bandit not installed"


# ── SecurityScanResult ────────────────────────────────────────────────────────


class TestSecurityScanResult:
    def _make_result(self, passed=True, severity=ScanSeverity.NONE):
        return SecurityScanResult(
            run_id=uuid.uuid4(),
            repo_path=Path("/tmp/repo"),
            scanned_at=datetime.now(UTC),
            overall_passed=passed,
            max_severity=severity,
            scans=[],
            blocking_reason=None if passed else "Issues found",
        )

    def test_as_dict_structure(self):
        r = self._make_result(passed=True)
        d = r.as_dict()
        assert "run_id" in d
        assert "repo_path" in d
        assert "scanned_at" in d
        assert d["overall_passed"] is True
        assert d["scans"] == []

    def test_as_dict_blocking_reason(self):
        r = self._make_result(passed=False, severity=ScanSeverity.HIGH)
        d = r.as_dict()
        assert d["overall_passed"] is False
        assert d["blocking_reason"] == "Issues found"

    def test_run_id_converted_to_string(self):
        r = self._make_result()
        d = r.as_dict()
        assert isinstance(d["run_id"], str)


# ── run_bandit ────────────────────────────────────────────────────────────────


class TestRunBandit:
    async def test_bandit_clean_output_passes(self, tmp_path):
        clean_output = json.dumps({"results": [], "errors": []})

        with patch(
            "phalanx.guardrails.security_pipeline._run_subprocess",
            AsyncMock(return_value=(0, clean_output, "")),
        ):
            result = await run_bandit(tmp_path)

        assert result.passed is True
        assert result.max_severity == ScanSeverity.NONE
        assert len(result.findings) == 0

    async def test_bandit_high_severity_fails(self, tmp_path):
        bandit_output = json.dumps(
            {
                "results": [
                    {
                        "test_id": "B105",
                        "issue_severity": "HIGH",
                        "filename": "app.py",
                        "line_number": 10,
                        "issue_text": "Hardcoded password",
                    }
                ],
                "errors": [],
            }
        )

        with patch(
            "phalanx.guardrails.security_pipeline._run_subprocess",
            AsyncMock(return_value=(1, bandit_output, "")),
        ):
            result = await run_bandit(tmp_path)

        assert result.passed is False
        assert result.max_severity == ScanSeverity.HIGH
        assert len(result.findings) == 1

    async def test_bandit_medium_severity_passes_gate(self, tmp_path):
        """MEDIUM findings are informational — gate only fails on HIGH+."""
        bandit_output = json.dumps(
            {
                "results": [
                    {
                        "test_id": "B201",
                        "issue_severity": "MEDIUM",
                        "filename": "app.py",
                        "line_number": 5,
                        "issue_text": "Flask debug mode",
                    }
                ]
            }
        )

        with patch(
            "phalanx.guardrails.security_pipeline._run_subprocess",
            AsyncMock(return_value=(1, bandit_output, "")),
        ):
            result = await run_bandit(tmp_path)

        assert result.passed is True
        assert result.max_severity == ScanSeverity.MEDIUM

    async def test_bandit_json_parse_failure_returns_error_result(self, tmp_path):
        with patch(
            "phalanx.guardrails.security_pipeline._run_subprocess",
            AsyncMock(return_value=(1, "not json", "some error")),
        ):
            result = await run_bandit(tmp_path)

        assert result.passed is False
        assert result.error is not None

    async def test_bandit_empty_output_returns_passed(self, tmp_path):
        with patch(
            "phalanx.guardrails.security_pipeline._run_subprocess",
            AsyncMock(return_value=(0, "", "")),
        ):
            result = await run_bandit(tmp_path)

        assert result.passed is True
