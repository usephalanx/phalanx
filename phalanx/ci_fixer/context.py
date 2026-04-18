"""
CIFixContext — shared state object for the multi-agent CI fix pipeline.

Every agent in the pipeline reads from and writes to this object.
It is persisted as JSON in CIFixRun.pipeline_context_json so the full
pipeline state is inspectable at any point via the API.

Design:
  - Dataclass with optional fields — agents populate their slice and leave
    the rest None until reached
  - Serializable to/from dict (JSON) — no custom encoder needed
  - Immutable agent outputs — each stage replaces its field entirely,
    never mutates in place
  - Final status is terminal — once set, no agent should write further

Agent → field mapping:
  Log Analyst       → structured_failure
  Root Cause Agent  → classified_failure
  Sandbox Prov.     → sandbox_id, sandbox_stack
  Reproducer Agent  → reproduction_result
  Fix Agent         → verified_patch
  Verifier Agent    → verification_result
  Commit Agent      → fix_commit_sha, fix_pr_number, fix_pr_url
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# ── Sub-objects (one per agent output) ────────────────────────────────────────


@dataclass
class StructuredFailure:
    """Output of Log Analyst — structured representation of the CI failure."""

    tool: str
    """Tool that failed: 'ruff', 'pytest', 'mypy', 'tsc', 'eslint', etc."""

    failure_type: str
    """Category: 'lint', 'type_error', 'test_regression', 'build', 'dependency', 'unknown'"""

    reproducer_cmd: str
    """Exact command CI ran: 'ruff check phalanx/ tests/ --output-format=github'"""

    errors: list[dict[str, Any]] = field(default_factory=list)
    """Parsed errors — list of {file, line, col, code, message} dicts"""

    failing_files: list[str] = field(default_factory=list)
    """File paths mentioned in the failure"""

    log_excerpt: str = ""
    """Relevant section of the raw CI log"""

    confidence: float = 1.0
    """Parser confidence 0.0–1.0"""


@dataclass
class ClassifiedFailure:
    """Output of Root Cause Agent — classification + escalation decision."""

    tier: Literal["L1_auto", "L2_escalate"]
    """L1 = auto-fixable; L2 = needs human"""

    root_cause: str
    """Human-readable root cause hypothesis"""

    stack: str
    """Detected tech stack: 'python', 'node', 'go', 'java', 'rust', 'unknown'"""

    confidence: float = 1.0
    """Classification confidence 0.0–1.0"""

    escalation_reason: str = ""
    """Populated when tier == L2 — why we're not attempting auto-fix"""


@dataclass
class ReproductionResult:
    """Output of Reproducer Agent — did we confirm the failure in sandbox?"""

    verdict: Literal["confirmed", "flaky", "env_mismatch", "timeout", "skipped"]
    """
    confirmed    — sandbox reproduced the same failure
    flaky        — command passed in sandbox → likely transient CI issue
    env_mismatch — command failed with a DIFFERENT error → wrong environment
    timeout      — sandbox command timed out
    skipped      — sandbox not available (Phase 1 fallback)
    """

    exit_code: int = -1
    output: str = ""
    reproducer_cmd: str = ""


@dataclass
class VerifiedPatch:
    """Output of Fix Agent — patch that has been validated locally."""

    files_modified: list[str] = field(default_factory=list)
    validation_cmd: str = ""
    validation_output: str = ""
    success: bool = False
    turns_used: int = 0


@dataclass
class VerificationResult:
    """Output of Verifier Agent — does the app/tests still work after the fix?"""

    verdict: Literal["passed", "failed", "skipped", "timeout"]
    output: str = ""
    cmd_run: str = ""


# ── Main context object ────────────────────────────────────────────────────────


@dataclass
class CIFixContext:
    """
    Shared state object for the multi-agent CI fix pipeline.

    Persisted as JSON in CIFixRun.pipeline_context_json.
    All fields except the identity fields are optional — populated
    as each agent completes its work.
    """

    # ── Identity (always set at pipeline start) ────────────────────────────
    ci_fix_run_id: str
    repo: str
    branch: str
    commit_sha: str
    original_build_id: str

    # ── Agent outputs (None until that agent runs) ─────────────────────────
    structured_failure: StructuredFailure | None = None
    classified_failure: ClassifiedFailure | None = None

    sandbox_id: str | None = None
    sandbox_stack: str | None = None

    reproduction_result: ReproductionResult | None = None
    verified_patch: VerifiedPatch | None = None
    verification_result: VerificationResult | None = None

    # ── Commit Agent output ────────────────────────────────────────────────
    fix_commit_sha: str | None = None
    fix_pr_number: int | None = None
    fix_pr_url: str | None = None
    fix_branch: str | None = None
    pr_was_existing: bool = False
    """True if the Commit Agent pushed to an existing fix PR rather than opening a new one."""

    # ── Pipeline metadata ──────────────────────────────────────────────────
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    final_status: Literal[
        "fixed", "escalated", "flaky", "env_mismatch", "failed", "in_progress"
    ] = "in_progress"
    pr_comment_posted: bool = False
    error: str | None = None

    # ── Serialisation ──────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CIFixContext:
        """Deserialize from a dict (as stored in pipeline_context_json)."""
        ctx = cls(
            ci_fix_run_id=d["ci_fix_run_id"],
            repo=d["repo"],
            branch=d["branch"],
            commit_sha=d["commit_sha"],
            original_build_id=d["original_build_id"],
        )
        # Agent outputs
        if d.get("structured_failure"):
            ctx.structured_failure = StructuredFailure(**d["structured_failure"])
        if d.get("classified_failure"):
            ctx.classified_failure = ClassifiedFailure(**d["classified_failure"])
        if d.get("reproduction_result"):
            ctx.reproduction_result = ReproductionResult(**d["reproduction_result"])
        if d.get("verified_patch"):
            ctx.verified_patch = VerifiedPatch(**d["verified_patch"])
        if d.get("verification_result"):
            ctx.verification_result = VerificationResult(**d["verification_result"])
        # Scalars
        ctx.sandbox_id = d.get("sandbox_id")
        ctx.sandbox_stack = d.get("sandbox_stack")
        ctx.fix_commit_sha = d.get("fix_commit_sha")
        ctx.fix_pr_number = d.get("fix_pr_number")
        ctx.fix_pr_url = d.get("fix_pr_url")
        ctx.fix_branch = d.get("fix_branch")
        ctx.pr_was_existing = d.get("pr_was_existing", False)
        ctx.started_at = d.get("started_at", ctx.started_at)
        ctx.completed_at = d.get("completed_at")
        ctx.final_status = d.get("final_status", "in_progress")
        ctx.pr_comment_posted = d.get("pr_comment_posted", False)
        ctx.error = d.get("error")
        return ctx

    def complete(
        self,
        status: Literal["fixed", "escalated", "flaky", "env_mismatch", "failed"],
        error: str | None = None,
    ) -> None:
        """Mark the pipeline as complete with a terminal status."""
        self.final_status = status
        self.completed_at = datetime.now(UTC).isoformat()
        if error:
            self.error = error

    @property
    def is_complete(self) -> bool:
        return self.final_status != "in_progress"

    @property
    def current_stage(self) -> str:
        """Human-readable name of the last completed stage."""
        if self.fix_commit_sha:
            return "committed"
        if self.verification_result:
            return "verified"
        if self.verified_patch:
            return "patched"
        if self.reproduction_result:
            return "reproduced"
        if self.sandbox_id:
            return "sandbox_ready"
        if self.classified_failure:
            return "classified"
        if self.structured_failure:
            return "parsed"
        return "started"
