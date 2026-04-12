"""
CI log fetchers — provider-specific adapters that fetch raw failure logs.

Each fetcher implements the LogFetcher protocol:
  async def fetch(event, api_key) -> str

The returned string is the raw failure log, truncated and focused on
the relevant failure section (last N lines of the failed step).

Phase 1: GitHub Actions + Buildkite
Phase 2: CircleCI + Jenkins
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import TYPE_CHECKING, Protocol

import httpx
import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.events import CIFailureEvent

log = structlog.get_logger(__name__)

# Max log characters to pass to the LLM — keep prompt manageable
_MAX_LOG_CHARS = 6000
# Lines to capture before the first error line for context
_CONTEXT_LINES_BEFORE = 10


class LogFetcher(Protocol):
    async def fetch(self, event: CIFailureEvent, api_key: str) -> str:
        """Fetch and return the failure log text for this CI event."""
        ...


# ── GitHub Actions ─────────────────────────────────────────────────────────────


class GitHubActionsLogFetcher:
    """
    Fetches CI logs from GitHub Actions.

    Strategy:
    1. Fetch check run annotations (inline error locations — most precise)
    2. Fetch the full log zip for the failed run and extract the failed step
    3. Combine annotations + relevant log section
    """

    async def fetch(self, event: CIFailureEvent, api_key: str) -> str:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        base = f"https://api.github.com/repos/{event.repo_full_name}"

        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Annotations (inline errors with file + line)
            annotations_text = ""
            try:
                r = await client.get(
                    f"{base}/check-runs/{event.build_id}/annotations",
                    headers=headers,
                )
                r.raise_for_status()
                annotations = r.json()
                if annotations:
                    lines = [
                        f"{a['path']}:{a['start_line']}: {a['message']}" for a in annotations[:20]
                    ]
                    annotations_text = "ANNOTATIONS:\n" + "\n".join(lines) + "\n\n"
            except Exception as exc:
                log.warning("ci_fixer.github.annotations_failed", error=str(exc))

            # 2. Job logs — fetch directly via the Jobs API (more reliable than log zip)
            log_text = ""
            try:
                # The check_run ID == the job ID in GitHub Actions
                # GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs returns plain text
                r = await client.get(
                    f"{base}/actions/jobs/{event.build_id}/logs",
                    headers=headers,
                    follow_redirects=True,
                )
                if r.status_code == 200:
                    raw_log = r.text
                    lines = raw_log.splitlines()
                    log_text = _extract_failure_section(lines)
                    log.info(
                        "ci_fixer.github.job_logs_fetched",
                        job_id=event.build_id,
                        lines=len(lines),
                    )
            except Exception as exc:
                log.warning("ci_fixer.github.logs_failed", error=str(exc))

            combined = annotations_text + log_text
            return _truncate(combined) if combined.strip() else "(no logs retrieved)"


def _extract_failed_step_from_zip(zip_bytes: bytes, failed_jobs: list[str]) -> str:
    """Extract the relevant failure section from a GitHub Actions log zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # Find files matching failed job names
            candidates: list[str] = []
            for name in zf.namelist():
                if any(job.lower() in name.lower() for job in (failed_jobs or [""])):
                    candidates.append(name)

            # Fall back to all files if no match
            if not candidates:
                candidates = zf.namelist()

            all_lines: list[str] = []
            for fname in candidates[:3]:
                with zf.open(fname) as f:
                    content = f.read().decode("utf-8", errors="replace")
                    all_lines.extend(content.splitlines())

            return _extract_failure_section(all_lines)
    except Exception:
        return ""


def _clean_log_lines(lines: list[str]) -> list[str]:
    """
    Strip GitHub Actions timestamps, ANSI codes, and known noise lines.
    Returns clean lines suitable for classification and file extraction.
    """
    # GitHub Actions prepends timestamps: "2026-04-12T17:36:04.1234567Z "
    timestamp_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*")
    # ANSI escape codes
    ansi_re = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
    # Known noise patterns to skip entirely
    noise_re = re.compile(
        r"(Node\.js \d+ actions are deprecated"
        r"|FORCE_JAVASCRIPT_ACTIONS_TO_NODE"
        r"|Set up job"
        r"|Complete job"
        r"|Post\s"
        r"|##\[group\]"
        r"|##\[endgroup\]"
        r"|##\[debug\]"
        r"|^$)",
        re.IGNORECASE,
    )
    cleaned = []
    for line in lines:
        line = timestamp_re.sub("", line)
        line = ansi_re.sub("", line)
        if not noise_re.search(line):
            cleaned.append(line)
    return cleaned


def _extract_failure_section(lines: list[str]) -> str:
    """
    Find the failure section in log lines.
    Cleans noise first, then finds the most specific failure block.
    Prefers tool-specific error patterns (ruff, pytest, mypy) over generic 'error'.
    """
    lines = _clean_log_lines(lines)

    # Priority patterns — find the most specific failure first
    priority_patterns = [
        re.compile(r"[\w/\.\-]+\.py:\d+:\d+:\s+[A-Z]\d+"),  # ruff: file:line:col: CODE
        re.compile(r"FAILED tests/"),  # pytest
        re.compile(r"[\w/\.\-]+\.py:\d+: error:"),  # mypy
        re.compile(r"error TS\d+"),  # tsc
        re.compile(r"Found \d+ error"),  # ruff summary
    ]

    for pattern in priority_patterns:
        for i, line in enumerate(lines):
            if pattern.search(line):
                start = max(0, i - _CONTEXT_LINES_BEFORE)
                section = lines[start : i + 100]
                return "\n".join(section)

    # Fallback: generic error keyword
    error_re = re.compile(r"\b(error|FAILED|Exception)\b", re.IGNORECASE)
    for i, line in enumerate(lines):
        if error_re.search(line):
            start = max(0, i - _CONTEXT_LINES_BEFORE)
            section = lines[start : i + 100]
            return "\n".join(section)

    # Last resort — return last 150 clean lines
    return "\n".join(lines[-150:])


# ── Buildkite ──────────────────────────────────────────────────────────────────


class BuildkiteLogFetcher:
    """
    Fetches CI logs from Buildkite REST API.

    Strategy:
    1. GET /builds/{org}/{pipeline}/{build_number}/jobs — find failed jobs
    2. GET /jobs/{job_id}/log — fetch raw log for each failed job
    """

    async def fetch(self, event: CIFailureEvent, api_key: str) -> str:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Buildkite build_id format: "org/pipeline/build_number"
        # OR just a UUID build ID — we handle both
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                # Get build details to find failed jobs
                r = await client.get(
                    f"https://api.buildkite.com/v2/builds/{event.build_id}",
                    headers=headers,
                )
                r.raise_for_status()
                build = r.json()

                failed_jobs = [
                    j
                    for j in build.get("jobs", [])
                    if j.get("state") in ("failed", "timed_out", "broken")
                ]

                logs: list[str] = []
                for job in failed_jobs[:3]:
                    job_id = job["id"]
                    try:
                        log_r = await client.get(
                            f"https://api.buildkite.com/v2/builds/{event.build_id}/jobs/{job_id}/log",
                            headers=headers,
                        )
                        log_r.raise_for_status()
                        job_log = log_r.json().get("content", "")
                        # Strip ANSI escape codes
                        job_log = re.sub(r"\x1b\[[0-9;]*m", "", job_log)
                        lines = job_log.splitlines()
                        section = _extract_failure_section(lines)
                        logs.append(f"JOB: {job.get('name', job_id)}\n{section}")
                    except Exception as exc:
                        log.warning(
                            "ci_fixer.buildkite.job_log_failed", job_id=job_id, error=str(exc)
                        )

                combined = "\n\n---\n\n".join(logs)
                return _truncate(combined) if combined.strip() else "(no logs retrieved)"

            except Exception as exc:
                log.warning("ci_fixer.buildkite.fetch_failed", error=str(exc))
                return "(log fetch failed)"


# ── CircleCI ───────────────────────────────────────────────────────────────────


class CircleCILogFetcher:
    """
    Fetches CI logs from CircleCI v2 API.
    Phase 2 — stub for now.
    """

    async def fetch(self, event: CIFailureEvent, api_key: str) -> str:
        # TODO Phase 2: implement CircleCI v2 API log fetch
        # GET /pipeline/{pipeline_id}/workflow
        # GET /workflow/{workflow_id}/job
        # GET /project/{slug}/job/{job_number}/steps
        log.warning("ci_fixer.circleci.not_implemented")
        return "(CircleCI log fetch not yet implemented)"


# ── Jenkins ────────────────────────────────────────────────────────────────────


class JenkinsLogFetcher:
    """
    Fetches CI logs from Jenkins REST API.
    Phase 2 — stub for now.
    """

    async def fetch(self, event: CIFailureEvent, api_key: str) -> str:
        # TODO Phase 2: implement Jenkins log fetch
        # GET {build_url}/consoleText  (api_key = "user:token" base64)  # pragma: allowlist secret
        log.warning("ci_fixer.jenkins.not_implemented")
        return "(Jenkins log fetch not yet implemented)"


# ── Registry ───────────────────────────────────────────────────────────────────

_FETCHERS: dict[str, LogFetcher] = {
    "github_actions": GitHubActionsLogFetcher(),
    "buildkite": BuildkiteLogFetcher(),
    "circleci": CircleCILogFetcher(),
    "jenkins": JenkinsLogFetcher(),
}


def get_log_fetcher(provider: str) -> LogFetcher:
    """Return the log fetcher for a CI provider. Raises KeyError if unknown."""
    if provider not in _FETCHERS:
        raise KeyError(f"Unknown CI provider: {provider!r}. Supported: {list(_FETCHERS)}")
    return _FETCHERS[provider]


# ── Helpers ────────────────────────────────────────────────────────────────────


def _truncate(text: str) -> str:
    """Truncate log text to fit within LLM prompt budget."""
    if len(text) <= _MAX_LOG_CHARS:
        return text
    # Keep the tail — failure details are usually at the end
    half = _MAX_LOG_CHARS // 2
    return text[:half] + "\n\n[... log truncated ...]\n\n" + text[-half:]
