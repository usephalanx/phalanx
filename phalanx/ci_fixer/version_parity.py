"""
CI Fix Tool Version Parity — Phase 4.

Before applying a fix, verify that the tool version currently installed in the
workspace matches the version that generated the original CI failure.  A version
mismatch (e.g. ruff 0.3.x failing but workspace has ruff 0.5.x) may mean the
failure was already fixed by a tool upgrade — or that our patch was developed
against different semantics.

Parity is checked at MINOR version level:
  - ruff 0.4.1 vs ruff 0.4.2 → OK (patch-level diff)
  - ruff 0.4.x vs ruff 0.5.x → NOT OK (minor-level diff — behavior may differ)
  - ruff 0.4.1 vs mypy 1.0.0 → N/A (different tools)

Returns a VersionParityResult with:
  ok: True if parity holds or check was not applicable
  local_version: what's installed in the workspace
  failure_version: what generated the original failure
  reason: human-readable explanation

Used by CIFixerAgent to set tool_version_parity_ok on the CIFixRun record and
surface in the PR body.  A mismatch does NOT block the fix — it adds a warning
to the PR body so reviewers can make an informed decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# Pattern: "ruff 0.4.1" or "mypy 1.10.0" or "pytest 8.2.0"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class VersionParityResult:
    ok: bool
    local_version: str
    failure_version: str
    reason: str


def check_version_parity(
    local_version: str,
    failure_version: str,
) -> VersionParityResult:
    """
    Compare tool versions at minor-version level.

    Args:
        local_version:   version string from the current workspace (e.g. "ruff 0.4.1")
        failure_version: version string captured when the CI failure occurred

    Returns VersionParityResult with ok=True if:
      - Either version string is empty/unknown (can't compare → assume OK)
      - Both are for the same tool and differ only at patch level
    """
    if not local_version or not failure_version:
        return VersionParityResult(
            ok=True,
            local_version=local_version,
            failure_version=failure_version,
            reason="version unavailable — parity check skipped",
        )

    # Extract tool name (first word)
    local_tool = local_version.split()[0] if local_version else ""
    failure_tool = failure_version.split()[0] if failure_version else ""

    # If tools differ, not comparable
    if local_tool and failure_tool and local_tool.lower() != failure_tool.lower():
        return VersionParityResult(
            ok=True,
            local_version=local_version,
            failure_version=failure_version,
            reason=f"different tools ({local_tool} vs {failure_tool}) — parity check not applicable",
        )

    local_match = _VERSION_RE.search(local_version)
    failure_match = _VERSION_RE.search(failure_version)

    if not local_match or not failure_match:
        return VersionParityResult(
            ok=True,
            local_version=local_version,
            failure_version=failure_version,
            reason="could not parse version numbers — parity check skipped",
        )

    local_major, local_minor, _ = int(local_match[1]), int(local_match[2]), int(local_match[3])
    fail_major, fail_minor, _ = int(failure_match[1]), int(failure_match[2]), int(failure_match[3])

    if local_major != fail_major or local_minor != fail_minor:
        reason = (
            f"minor version mismatch: local={local_version}, failure={failure_version}. "
            f"Fix was developed on {local_tool} {local_major}.{local_minor}.x but failure "
            f"occurred on {fail_major}.{fail_minor}.x — behavior may differ."
        )
        log.warning(
            "version_parity.mismatch",
            local=local_version,
            failure=failure_version,
        )
        return VersionParityResult(
            ok=False,
            local_version=local_version,
            failure_version=failure_version,
            reason=reason,
        )

    return VersionParityResult(
        ok=True,
        local_version=local_version,
        failure_version=failure_version,
        reason=f"versions match at minor level ({local_version})",
    )


def should_auto_merge(
    integration_auto_merge: bool,
    fingerprint_success_count: int,
    min_success_count: int,
    parity_ok: bool,
) -> bool:
    """
    Determine whether auto-merge should be enabled for a fix PR.

    All conditions must hold:
      1. Integration has auto_merge=True (explicit opt-in by repo owner)
      2. Fingerprint has >= min_success_count successful fixes (proven pattern)
      3. Tool version parity is OK (no minor version mismatch)

    Returns False if any condition fails.
    """
    if not integration_auto_merge:
        return False

    if fingerprint_success_count < min_success_count:
        log.debug(
            "auto_merge.insufficient_history",
            success_count=fingerprint_success_count,
            required=min_success_count,
        )
        return False

    if not parity_ok:
        log.debug("auto_merge.parity_mismatch_blocked")
        return False

    return True


def format_parity_notice(parity: VersionParityResult) -> str:
    """Format a human-readable parity notice for the PR body."""
    if parity.ok:
        if parity.local_version:
            return f"✅ Tool version parity OK: `{parity.local_version}`"
        return "✅ Tool version parity: not checked (version unavailable)"
    return (
        f"⚠️ **Tool version mismatch**\n"
        f"- Failure was detected with: `{parity.failure_version}`\n"
        f"- Fix was developed with: `{parity.local_version}`\n"
        f"- {parity.reason}\n\n"
        f"Please verify the fix is correct for your installed version."
    )
