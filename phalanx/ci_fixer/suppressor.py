"""
CI Fix Flaky Suppressor — Phase 3 pre-analysis gate.

Before calling the LLM analyst, check whether the errors in the parsed log
are historically "flaky" for this repo.  A flaky pattern is one that has
self-healed (passed on retry without any code change) more than 50% of the
time historically.

If ALL errors in the log are high-flakiness patterns, suppress the fix attempt.
The run is marked SUPPRESSED (a new terminal status introduced in Phase 3).

This prevents Phalanx from wasting LLM calls on transient failures like:
  - Docker Hub rate limits (lint tools can't download their own deps)
  - Flaky network tests
  - Race conditions in the test suite that self-heal on retry

Suppressor also contains history weighting logic:
  _should_use_history(fingerprint) → True only when success_count > failure_count
  This prevents Phase 2 history from reusing bad patches on failures that are
  structurally similar but contextually different.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.log_parser import ParsedLog
    from phalanx.db.models import CIFailureFingerprint, CIFlakyPattern

log = structlog.get_logger(__name__)

# Flakiness threshold: if a pattern self-heals >= this fraction of the time,
# it is considered high-flakiness and suppressed.
_FLAKY_THRESHOLD = 0.5
# Minimum observations required before we trust the flakiness rate.
# With < 3 observations, we don't suppress — too little signal.
_MIN_OBSERVATIONS = 3


def is_flaky_suppressed(
    parsed_log: ParsedLog,
    flaky_patterns: list[CIFlakyPattern],
) -> bool:
    """
    Return True if ALL errors in parsed_log are high-flakiness patterns.

    If even one error is NOT historically flaky, we proceed with the fix.
    This is intentionally conservative — we'd rather attempt an unnecessary
    fix than suppress a legitimate one.

    Args:
        parsed_log: the deterministically parsed CI failure log.
        flaky_patterns: CIFlakyPattern rows for this repo (loaded by caller).
    """
    if not flaky_patterns:
        return False

    errors = list(parsed_log.lint_errors) + list(parsed_log.type_errors)
    if not errors:
        # Test failures — don't suppress based on flakiness (too risky)
        return False

    # Build lookup: (file, code) → CIFlakyPattern
    pattern_map: dict[tuple[str, str], CIFlakyPattern] = {}
    for p in flaky_patterns:
        key = (p.error_file or "", p.error_code or "")
        pattern_map[key] = p

    for error in errors:
        file = getattr(error, "file", "")
        code = getattr(error, "code", "") or ""
        key = (file, code)
        pattern = pattern_map.get(key)

        if pattern is None:
            # Unknown pattern → not suppressed
            log.debug("suppressor.unknown_pattern", file=file, code=code)
            return False

        if pattern.total_count < _MIN_OBSERVATIONS:
            # Too few observations → not suppressed
            log.debug(
                "suppressor.insufficient_observations",
                file=file,
                code=code,
                total_count=pattern.total_count,
            )
            return False

        if pattern.flaky_rate < _FLAKY_THRESHOLD:
            # Below threshold → not suppressed
            log.debug(
                "suppressor.below_threshold",
                file=file,
                code=code,
                flaky_rate=pattern.flaky_rate,
            )
            return False

    # All errors are high-flakiness patterns
    log.info(
        "suppressor.all_flaky",
        error_count=len(errors),
        pattern_count=len(flaky_patterns),
    )
    return True


def should_use_history(fingerprint: CIFailureFingerprint | None) -> bool:
    """
    Return True if the fingerprint's history is trustworthy enough to reuse.

    Phase 3 history weighting: only reuse a cached patch if it has succeeded
    more times than it has failed.  A fingerprint with 1 success and 3 failures
    is not reliable — the patch probably only worked in a specific context.

    Returns False (don't reuse) when:
      - fingerprint is None (no history at all)
      - failure_count >= success_count (more failures than successes)
      - last_good_patch_json is absent (nothing to reuse)
    """
    if fingerprint is None:
        return False

    if not fingerprint.last_good_patch_json:
        return False

    if fingerprint.success_count <= fingerprint.failure_count:
        log.debug(
            "suppressor.history_unreliable",
            fingerprint=fingerprint.fingerprint_hash,
            success=fingerprint.success_count,
            failure=fingerprint.failure_count,
        )
        return False

    return True


def record_flaky_pattern(
    repo_full_name: str,
    tool: str,
    error_code: str | None,
    error_file: str | None,
    was_flaky: bool,
    existing_pattern: CIFlakyPattern | None = None,
) -> dict:
    """
    Return the dict of fields to set when upserting a CIFlakyPattern row.

    Caller is responsible for the actual DB write — this function is pure so
    it can be unit-tested without a DB.

    Args:
        repo_full_name: e.g. "acme/backend"
        tool: e.g. "ruff"
        error_code: e.g. "F401" or None
        error_file: normalised file path or None
        was_flaky: True if this occurrence self-healed (no fix needed)
        existing_pattern: existing ORM row if one exists, else None

    Returns:
        dict of field values to set (for ORM update or insert)
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    now = datetime.now(UTC)

    if existing_pattern is None:
        return {
            "repo_full_name": repo_full_name,
            "tool": tool,
            "error_code": error_code,
            "error_file": error_file,
            "flaky_count": 1 if was_flaky else 0,
            "total_count": 1,
            "first_seen_at": now,
            "last_seen_at": now,
        }

    new_total = existing_pattern.total_count + 1
    new_flaky = existing_pattern.flaky_count + (1 if was_flaky else 0)
    return {
        "flaky_count": new_flaky,
        "total_count": new_total,
        "last_seen_at": now,
    }
