"""v1.7.2.4 — Full-CI re-confirm gate for Commander.

After SRE Verify reports `all_green` against the engineer's narrow
verify_command, this gate poll-confirms GitHub's full check-runs on
the engineer's head SHA before Commander finalizes SHIP.

Decision matrix:
  TRUE_GREEN     — every previously-failing check is now success;
                   no previously-green check went red. SHIP.
  REGRESSION     — at least one check that was success at base_sha is
                   now failure at head_sha. Engineer's edit broke
                   something that wasn't broken before. REPLAN/ESCALATE.
  NOT_FIXED      — at least one check that was failure at base_sha is
                   still failure at head_sha. The narrow verify lied
                   (or TL targeted the wrong job). REPLAN/ESCALATE.
  PENDING_TIMEOUT — checks haven't all settled in poll_timeout_s.
                    Conservative: ESCALATE rather than ship blindly.
  MISSING_DATA   — GitHub returned no check-runs for one or both SHAs
                   (e.g. workflows still being scheduled). ESCALATE.

The gate is intentionally pure (returns a verdict; doesn't transition
state). Commander reads the verdict and decides REPLAN vs ESCALATE
based on its own iteration/fingerprint state.

Excluded checks: any check whose `name` starts with `cifix/` (Phalanx's
own bot-status check), or check IDs the caller passes in `ignore_check_ids`
(e.g. the failing_job_id we already targeted, since SRE Verify's
out-of-band run is the source of truth for that one).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict shape
# ─────────────────────────────────────────────────────────────────────────────


GateDecision = Literal[
    "TRUE_GREEN",
    "REGRESSION",
    "NOT_FIXED",
    "PENDING_TIMEOUT",
    "MISSING_DATA",
]


@dataclass
class CheckSummary:
    """One check-run's view at a single SHA. The fields we care about
    for the gate decision + escalation record."""
    name: str
    conclusion: str | None  # "success" | "failure" | "neutral" | "cancelled" | "skipped" | "timed_out" | None (in_progress)
    status: str             # "queued" | "in_progress" | "completed"
    html_url: str | None
    summary: str | None     # check_run.output.summary (first 500 chars)


@dataclass
class CheckGateVerdict:
    decision: GateDecision
    base_sha: str
    head_sha: str
    pre_checks: dict[str, CheckSummary] = field(default_factory=dict)
    post_checks: dict[str, CheckSummary] = field(default_factory=dict)
    fixed: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)
    still_failing: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    poll_seconds: int = 0
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialization for AgentResult.output / escalation_record."""
        return {
            "decision": self.decision,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "fixed": list(self.fixed),
            "regressed": list(self.regressed),
            "still_failing": list(self.still_failing),
            "pending": list(self.pending),
            "poll_seconds": self.poll_seconds,
            "notes": self.notes,
            "pre_checks": {
                k: {"conclusion": v.conclusion, "status": v.status, "html_url": v.html_url}
                for k, v in self.pre_checks.items()
            },
            "post_checks": {
                k: {
                    "conclusion": v.conclusion,
                    "status": v.status,
                    "html_url": v.html_url,
                    "summary": v.summary,
                }
                for k, v in self.post_checks.items()
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Pure decision logic (testable without GitHub)
# ─────────────────────────────────────────────────────────────────────────────


_TERMINAL_CONCLUSIONS: frozenset[str] = frozenset({
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out",
    "action_required", "stale",
})

# These conclusions count as "passed" for gate purposes. neutral and
# skipped are treated as non-failures (consistent with GitHub's required-
# check semantics). cancelled/timed_out/action_required count as fails.
_PASSING_CONCLUSIONS: frozenset[str] = frozenset({
    "success", "neutral", "skipped",
})


def _is_pending(c: CheckSummary) -> bool:
    """A check is pending if it has no conclusion yet (still queued/running)."""
    if c.conclusion is None:
        return True
    return c.conclusion not in _TERMINAL_CONCLUSIONS


def _is_failure(c: CheckSummary) -> bool:
    """Failure = terminal AND not in the passing set."""
    if _is_pending(c):
        return False
    return (c.conclusion or "") not in _PASSING_CONCLUSIONS


def _filter_checks(
    checks: dict[str, CheckSummary],
    *,
    ignore_prefixes: tuple[str, ...] = ("cifix/",),
    ignore_names: frozenset[str] | None = None,
) -> dict[str, CheckSummary]:
    """Drop our own bot-status checks + any names the caller wants ignored.
    Keeps the gate from gating on its own progress."""
    out: dict[str, CheckSummary] = {}
    ignore = ignore_names or frozenset()
    for name, c in checks.items():
        if name in ignore:
            continue
        if any(name.startswith(p) for p in ignore_prefixes):
            continue
        out[name] = c
    return out


def decide(
    *,
    base_checks: dict[str, CheckSummary],
    head_checks: dict[str, CheckSummary],
    base_sha: str,
    head_sha: str,
    ignore_check_names: frozenset[str] | None = None,
    poll_seconds: int = 0,
) -> CheckGateVerdict:
    """Pure decision function. Tier-1 testable.

    Filters out cifix/* checks first (always), then any names the caller
    explicitly requested to ignore. Compares base vs. head and returns a
    verdict.
    """
    base = _filter_checks(base_checks, ignore_names=ignore_check_names)
    head = _filter_checks(head_checks, ignore_names=ignore_check_names)

    verdict = CheckGateVerdict(
        decision="TRUE_GREEN",  # optimistic; downgraded below
        base_sha=base_sha,
        head_sha=head_sha,
        pre_checks=base,
        post_checks=head,
        poll_seconds=poll_seconds,
    )

    if not head:
        # No checks reported on head — workflows might not have started,
        # or the repo doesn't run any. Conservative: don't ship.
        verdict.decision = "MISSING_DATA"
        verdict.notes = "no check-runs returned for head_sha"
        return verdict

    # Pending checks block ship (caller should poll-loop until they
    # settle, then call decide() again with the latest snapshot).
    pending = [n for n, c in head.items() if _is_pending(c)]
    if pending:
        verdict.decision = "PENDING_TIMEOUT"
        verdict.pending = pending
        verdict.notes = f"{len(pending)} check(s) still pending after poll"
        return verdict

    # Compare base ↔ head per check name.
    fixed: list[str] = []
    regressed: list[str] = []
    still_failing: list[str] = []
    for name, head_c in head.items():
        base_c = base.get(name)
        if _is_failure(head_c):
            if base_c is not None and not _is_failure(base_c):
                # Was passing/neutral; now failing → regression
                regressed.append(name)
            else:
                # Was failing or absent; still failing
                still_failing.append(name)
        else:
            # head is success/neutral/skipped
            if base_c is not None and _is_failure(base_c):
                fixed.append(name)

    verdict.fixed = sorted(fixed)
    verdict.regressed = sorted(regressed)
    verdict.still_failing = sorted(still_failing)

    # Decision priority: regression > not-fixed > true-green
    # (Regression is the worst — we made the customer's repo worse.)
    if regressed:
        verdict.decision = "REGRESSION"
        verdict.notes = (
            f"{len(regressed)} previously-green check(s) regressed: "
            f"{', '.join(regressed[:5])}"
        )
    elif still_failing:
        verdict.decision = "NOT_FIXED"
        verdict.notes = (
            f"{len(still_failing)} previously-failing check(s) still failing: "
            f"{', '.join(still_failing[:5])}"
        )
    else:
        verdict.decision = "TRUE_GREEN"
        verdict.notes = (
            f"{len(fixed)} check(s) recovered, no regressions"
            if fixed else "all checks green; no pre-fix failures observed"
        )
    return verdict


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API client (poll loop + fetch)
# ─────────────────────────────────────────────────────────────────────────────


_GITHUB_API_BASE = "https://api.github.com"


async def _fetch_check_runs(
    *, repo: str, sha: str, github_token: str,
) -> dict[str, CheckSummary]:
    """GET /repos/{repo}/commits/{sha}/check-runs — paginate per_page=100.

    Returns name → latest-CheckSummary (latest-id-wins for duplicate names,
    since GitHub may surface re-runs as separate check_runs).
    """
    import httpx  # noqa: PLC0415

    url = f"{_GITHUB_API_BASE}/repos/{repo}/commits/{sha}/check-runs"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params: dict[str, Any] = {"per_page": 100}

    out_by_name: dict[str, tuple[int, CheckSummary]] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        page = 1
        while page <= 5:  # cap at 500 checks per sha — pathological if more
            params["page"] = page
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                log.warning(
                    "github_check_gate.fetch_failed sha=%s status=%d body=%s",
                    sha[:12], resp.status_code, resp.text[:200],
                )
                break
            body = resp.json()
            runs = body.get("check_runs") or []
            if not runs:
                break
            for r in runs:
                name = r.get("name") or "?"
                rid = int(r.get("id") or 0)
                summary = (r.get("output") or {}).get("summary") or None
                if summary:
                    summary = summary[:500]
                cs = CheckSummary(
                    name=name,
                    conclusion=r.get("conclusion"),
                    status=r.get("status") or "unknown",
                    html_url=r.get("html_url"),
                    summary=summary,
                )
                # Latest-id-wins for duplicate names (most recent re-run)
                prev = out_by_name.get(name)
                if prev is None or rid > prev[0]:
                    out_by_name[name] = (rid, cs)
            if len(runs) < params["per_page"]:
                break
            page += 1
    return {name: cs for name, (_, cs) in out_by_name.items()}


async def evaluate_check_gate(
    *,
    repo: str,
    github_token: str,
    base_sha: str,
    head_sha: str,
    ignore_check_names: frozenset[str] | None = None,
    poll_timeout_s: int = 300,
    poll_interval_s: int = 15,
) -> CheckGateVerdict:
    """Fetch base + head check-runs, poll until head settles or timeout.

    base_sha is fetched once (it shouldn't change). head_sha is polled
    on poll_interval_s until no checks are pending OR poll_timeout_s.

    Returns a CheckGateVerdict the commander can act on directly.
    """
    base_checks = await _fetch_check_runs(
        repo=repo, sha=base_sha, github_token=github_token,
    )

    elapsed = 0
    last_head: dict[str, CheckSummary] = {}
    while elapsed <= poll_timeout_s:
        head_checks = await _fetch_check_runs(
            repo=repo, sha=head_sha, github_token=github_token,
        )
        last_head = head_checks
        if head_checks:
            verdict = decide(
                base_checks=base_checks,
                head_checks=head_checks,
                base_sha=base_sha,
                head_sha=head_sha,
                ignore_check_names=ignore_check_names,
                poll_seconds=elapsed,
            )
            if verdict.decision != "PENDING_TIMEOUT":
                return verdict
        # else: empty — keep polling, GitHub may not have scheduled yet
        await asyncio.sleep(poll_interval_s)
        elapsed += poll_interval_s

    # Final pass with whatever we last saw
    return decide(
        base_checks=base_checks,
        head_checks=last_head,
        base_sha=base_sha,
        head_sha=head_sha,
        ignore_check_names=ignore_check_names,
        poll_seconds=elapsed,
    )


__all__ = [
    "CheckGateVerdict",
    "CheckSummary",
    "GateDecision",
    "decide",
    "evaluate_check_gate",
]
