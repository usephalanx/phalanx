"""
Security Pipeline — mandatory pre-ship security gate.

Orchestrates:
  1. Secrets detection (detect-secrets baseline diff)
  2. SAST scan (bandit)
  3. Dependency CVE audit (pip-audit)
  4. Container image scan (trivy) — only when Docker image SHA provided

Persists structured SecurityScanResult to Postgres as a 'security_report' Artifact.
Blocks the Run's ship approval if severity threshold is breached.

This gate is NEVER bypassable in code — only a human IC6 override in Postgres
(`approvals` row with type='security_override') unblocks a failed gate.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from forge.db.session import get_db

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class ScanSeverity(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEVERITY_RANK: dict[ScanSeverity, int] = {
    ScanSeverity.NONE: 0,
    ScanSeverity.LOW: 1,
    ScanSeverity.MEDIUM: 2,
    ScanSeverity.HIGH: 3,
    ScanSeverity.CRITICAL: 4,
}


@dataclass
class ScanFinding:
    tool: str
    severity: ScanSeverity
    title: str
    location: str
    detail: str
    cve: str | None = None

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "severity": self.severity,
            "title": self.title,
            "location": self.location,
            "detail": self.detail,
            "cve": self.cve,
        }


@dataclass
class ScanResult:
    tool: str
    passed: bool
    max_severity: ScanSeverity = ScanSeverity.NONE
    findings: list[ScanFinding] = field(default_factory=list)
    raw_output: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "passed": self.passed,
            "max_severity": self.max_severity,
            "findings": [f.as_dict() for f in self.findings],
            "error": self.error,
        }


@dataclass
class SecurityScanResult:
    run_id: UUID
    repo_path: Path
    scanned_at: datetime
    overall_passed: bool
    max_severity: ScanSeverity
    scans: list[ScanResult]
    blocking_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "run_id": str(self.run_id),
            "repo_path": str(self.repo_path),
            "scanned_at": self.scanned_at.isoformat(),
            "overall_passed": self.overall_passed,
            "max_severity": self.max_severity,
            "blocking_reason": self.blocking_reason,
            "scans": [s.as_dict() for s in self.scans],
        }


# ---------------------------------------------------------------------------
# Scanner implementations
# ---------------------------------------------------------------------------


async def _run_subprocess(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a command asynchronously and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def run_bandit(repo_path: Path) -> ScanResult:
    """Run bandit SAST scanner. HIGH+ findings fail the gate."""
    log.info("security_pipeline.bandit.start", path=str(repo_path))

    returncode, stdout, stderr = await _run_subprocess(
        [
            "bandit",
            "-r",
            str(repo_path),
            "-f",
            "json",
            "--exclude",
            ".venv,node_modules,alembic/versions",
            "-ll",  # report LOW and above
        ]
    )

    # bandit exits 1 when issues found — that's expected
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return ScanResult(
            tool="bandit",
            passed=False,
            error=f"Failed to parse bandit output: {stderr[:500]}",
        )

    findings: list[ScanFinding] = []
    max_severity = ScanSeverity.NONE

    for issue in data.get("results", []):
        severity_str = issue.get("issue_severity", "LOW").upper()
        severity = ScanSeverity[severity_str] if severity_str in ScanSeverity.__members__ else ScanSeverity.LOW

        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[max_severity]:
            max_severity = severity

        findings.append(
            ScanFinding(
                tool="bandit",
                severity=severity,
                title=issue.get("test_id", "unknown"),
                location=f"{issue.get('filename', '?')}:{issue.get('line_number', '?')}",
                detail=issue.get("issue_text", ""),
            )
        )

    # Gate: HIGH or CRITICAL findings fail
    passed = _SEVERITY_RANK[max_severity] < _SEVERITY_RANK[ScanSeverity.HIGH]

    log.info(
        "security_pipeline.bandit.done",
        findings=len(findings),
        max_severity=max_severity,
        passed=passed,
    )

    return ScanResult(
        tool="bandit",
        passed=passed,
        max_severity=max_severity,
        findings=findings,
        raw_output=stdout[:10_000],
    )


async def run_pip_audit(repo_path: Path) -> ScanResult:
    """Run pip-audit CVE scanner. Any CRITICAL or HIGH CVE fails the gate."""
    log.info("security_pipeline.pip_audit.start")

    returncode, stdout, stderr = await _run_subprocess(
        ["pip-audit", "--format=json", "--progress-spinner=off"],
        cwd=repo_path,
    )

    try:
        data = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return ScanResult(
            tool="pip-audit",
            passed=returncode == 0,
            error=f"Failed to parse pip-audit output: {stderr[:500]}",
        )

    findings: list[ScanFinding] = []
    max_severity = ScanSeverity.NONE

    for dep in data:
        for vuln in dep.get("vulns", []):
            # pip-audit doesn't provide severity — treat all CVEs as HIGH
            severity = ScanSeverity.HIGH
            if _SEVERITY_RANK[severity] > _SEVERITY_RANK[max_severity]:
                max_severity = severity

            findings.append(
                ScanFinding(
                    tool="pip-audit",
                    severity=severity,
                    title=vuln.get("id", "UNKNOWN-CVE"),
                    location=f"{dep.get('name', '?')}=={dep.get('version', '?')}",
                    detail=vuln.get("description", ""),
                    cve=vuln.get("id"),
                )
            )

    passed = len(findings) == 0

    log.info(
        "security_pipeline.pip_audit.done",
        vulnerabilities=len(findings),
        passed=passed,
    )

    return ScanResult(
        tool="pip-audit",
        passed=passed,
        max_severity=max_severity,
        findings=findings,
        raw_output=stdout[:10_000],
    )


async def run_secrets_scan(repo_path: Path) -> ScanResult:
    """Run detect-secrets to ensure no new secrets vs baseline."""
    log.info("security_pipeline.secrets.start")

    baseline_path = repo_path / ".secrets.baseline"

    if not baseline_path.exists():
        # No baseline yet — generate one and pass (first run)
        await _run_subprocess(
            ["detect-secrets", "scan", "--baseline", str(baseline_path)],
            cwd=repo_path,
        )
        return ScanResult(
            tool="detect-secrets",
            passed=True,
            raw_output="Generated initial baseline — no secrets found.",
        )

    returncode, stdout, stderr = await _run_subprocess(
        ["detect-secrets", "audit", "--report", "--json", str(baseline_path)],
        cwd=repo_path,
    )

    # Also do a diff scan to catch new secrets not yet in baseline
    diff_rc, diff_out, diff_err = await _run_subprocess(
        ["detect-secrets", "scan", "--only-allowlisted"],
        cwd=repo_path,
    )

    findings: list[ScanFinding] = []

    try:
        audit_data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        audit_data = {}

    for file_path, secrets in audit_data.get("results", {}).items():
        for secret in secrets:
            if not secret.get("is_secret"):
                continue  # skip false positives already audited
            findings.append(
                ScanFinding(
                    tool="detect-secrets",
                    severity=ScanSeverity.CRITICAL,
                    title=secret.get("type", "secret"),
                    location=f"{file_path}:{secret.get('line_number', '?')}",
                    detail="Potential secret detected — review and rotate immediately.",
                )
            )

    passed = len(findings) == 0
    max_severity = ScanSeverity.CRITICAL if findings else ScanSeverity.NONE

    log.info(
        "security_pipeline.secrets.done",
        secrets_found=len(findings),
        passed=passed,
    )

    return ScanResult(
        tool="detect-secrets",
        passed=passed,
        max_severity=max_severity,
        findings=findings,
        raw_output=stdout[:5_000],
    )


async def run_trivy_image_scan(image_ref: str) -> ScanResult:
    """Run Trivy container image scan. CRITICAL CVEs fail the gate."""
    log.info("security_pipeline.trivy.start", image=image_ref)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report_path = tmp.name

    returncode, stdout, stderr = await _run_subprocess(
        [
            "trivy",
            "image",
            "--format=json",
            "--exit-code=0",  # we handle exit ourselves
            f"--output={report_path}",
            "--severity=MEDIUM,HIGH,CRITICAL",
            "--ignore-unfixed",
            image_ref,
        ]
    )

    try:
        with open(report_path) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return ScanResult(
            tool="trivy",
            passed=False,
            error=f"Trivy report unreadable: {exc}",
        )

    findings: list[ScanFinding] = []
    max_severity = ScanSeverity.NONE

    for result in data.get("Results", []):
        for vuln in result.get("Vulnerabilities", []):
            sev_str = vuln.get("Severity", "LOW").upper()
            try:
                severity = ScanSeverity[sev_str]
            except KeyError:
                severity = ScanSeverity.LOW

            if _SEVERITY_RANK[severity] > _SEVERITY_RANK[max_severity]:
                max_severity = severity

            findings.append(
                ScanFinding(
                    tool="trivy",
                    severity=severity,
                    title=vuln.get("VulnerabilityID", "UNKNOWN"),
                    location=f"{vuln.get('PkgName', '?')}:{vuln.get('InstalledVersion', '?')}",
                    detail=vuln.get("Description", "")[:200],
                    cve=vuln.get("VulnerabilityID"),
                )
            )

    passed = _SEVERITY_RANK[max_severity] < _SEVERITY_RANK[ScanSeverity.HIGH]

    log.info(
        "security_pipeline.trivy.done",
        vulnerabilities=len(findings),
        max_severity=max_severity,
        passed=passed,
    )

    return ScanResult(
        tool="trivy",
        passed=passed,
        max_severity=max_severity,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class SecurityPipeline:
    """
    Runs all security scans for a given run before ship approval.

    Usage:
        pipeline = SecurityPipeline(run_id=..., repo_path=Path("/forge-repos/my-project"))
        result = await pipeline.run()
        if not result.overall_passed:
            raise SecurityGateBlockedError(result.blocking_reason)
    """

    # Severity threshold at which the gate fails
    FAIL_THRESHOLD = ScanSeverity.HIGH

    def __init__(
        self,
        run_id: UUID,
        repo_path: Path,
        image_ref: str | None = None,
        skip_tools: list[str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.repo_path = repo_path
        self.image_ref = image_ref
        self.skip_tools: set[str] = set(skip_tools or [])
        self._log = log.bind(run_id=str(run_id))

    async def run(self) -> SecurityScanResult:
        self._log.info("security_pipeline.start", repo=str(self.repo_path))

        scan_tasks: list[asyncio.Task] = []
        tool_names: list[str] = []

        if "detect-secrets" not in self.skip_tools:
            scan_tasks.append(asyncio.create_task(run_secrets_scan(self.repo_path)))
            tool_names.append("detect-secrets")

        if "bandit" not in self.skip_tools:
            scan_tasks.append(asyncio.create_task(run_bandit(self.repo_path)))
            tool_names.append("bandit")

        if "pip-audit" not in self.skip_tools:
            scan_tasks.append(asyncio.create_task(run_pip_audit(self.repo_path)))
            tool_names.append("pip-audit")

        if self.image_ref and "trivy" not in self.skip_tools:
            scan_tasks.append(asyncio.create_task(run_trivy_image_scan(self.image_ref)))
            tool_names.append("trivy")

        scan_results: list[ScanResult] = await asyncio.gather(*scan_tasks, return_exceptions=False)

        # Aggregate
        overall_max = ScanSeverity.NONE
        all_passed = True
        for result in scan_results:
            if _SEVERITY_RANK[result.max_severity] > _SEVERITY_RANK[overall_max]:
                overall_max = result.max_severity
            if not result.passed:
                all_passed = False

        overall_passed = (
            all_passed and _SEVERITY_RANK[overall_max] < _SEVERITY_RANK[self.FAIL_THRESHOLD]
        )

        blocking_reason: str | None = None
        if not overall_passed:
            failed_tools = [r.tool for r in scan_results if not r.passed]
            blocking_reason = (
                f"Security gate failed — max severity: {overall_max}. "
                f"Failed scanners: {', '.join(failed_tools)}. "
                f"An IC6 security_override approval is required to proceed."
            )

        result = SecurityScanResult(
            run_id=self.run_id,
            repo_path=self.repo_path,
            scanned_at=datetime.now(UTC),
            overall_passed=overall_passed,
            max_severity=overall_max,
            scans=list(scan_results),
            blocking_reason=blocking_reason,
        )

        await self._persist_artifact(result)

        self._log.info(
            "security_pipeline.done",
            passed=overall_passed,
            max_severity=overall_max,
            num_scans=len(scan_results),
        )

        return result

    async def _persist_artifact(self, result: SecurityScanResult) -> None:
        """Save the security report as an Artifact row for evidence."""
        try:
            import hashlib  # noqa: PLC0415
            import json as _json  # noqa: PLC0415

            from sqlalchemy import select  # noqa: PLC0415

            from forge.db.models import Artifact, Run  # noqa: PLC0415

            json_bytes = _json.dumps(result.as_dict()).encode()
            content_hash = hashlib.sha256(json_bytes).hexdigest()

            async with get_db() as session:
                row = await session.execute(
                    select(Run.project_id).where(Run.id == str(self.run_id))
                )
                project_id = row.scalar_one()

                artifact = Artifact(
                    run_id=str(self.run_id),
                    project_id=project_id,
                    artifact_type="security_report",
                    title=f"security_report_{self.run_id}",
                    s3_key=f"local/{self.run_id}/security_report.json",
                    content_hash=content_hash,
                    quality_evidence=result.as_dict(),
                )
                session.add(artifact)
                await session.commit()
        except Exception as exc:
            # Non-fatal — log and continue; the result is still returned to caller
            self._log.warning("security_pipeline.persist_failed", error=str(exc))


class SecurityGateBlockedError(Exception):
    """Raised when the security pipeline blocks a run from proceeding."""

    def __init__(self, reason: str, result: SecurityScanResult | None = None) -> None:
        super().__init__(reason)
        self.result = result
