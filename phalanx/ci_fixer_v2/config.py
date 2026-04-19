"""CI Fixer v2 — constants and enum types.

Single source of truth for loop limits, escalation reasons, and trace types.
All magic numbers from the spec live here; if a number needs changing, change
it in one place and update the spec reference in docs/ci-fixer-v2-spec.md.
"""

from __future__ import annotations

from enum import Enum

# ── Loop discipline (spec §6) ─────────────────────────────────────────────
# Hard turn cap for the main agent. If the agent does not commit or escalate
# before this is reached, it auto-escalates (no silent exits).
MAX_MAIN_TURNS: int = 25

# Hard turn cap inside the delegate_to_coder subagent (spec §5).
MAX_SUBAGENT_TURNS: int = 10

# Default retries the coder subagent attempts at applying a patch + verifying
# before returning success=False to the main agent.
DEFAULT_CODER_MAX_ATTEMPTS: int = 3

# Extended-thinking token budget for the Sonnet coder subagent (spec §5).
SONNET_THINKING_BUDGET: int = 4000


# ── Escalation reasons (spec §4.5) ────────────────────────────────────────
class EscalationReason(str, Enum):
    """Why the agent chose to escalate instead of commit.

    Kept as a closed set so downstream telemetry / dashboards can bucket
    outcomes reliably.
    """

    LOW_CONFIDENCE = "low_confidence"
    TURN_CAP_REACHED = "turn_cap_reached"
    AMBIGUOUS_FIX = "ambiguous_fix"
    PREEXISTING_MAIN_FAILURE = "preexisting_main_failure"
    INFRA_FAILURE_OUT_OF_SCOPE = "infra_failure_out_of_scope"
    DESTRUCTIVE_CHANGE_REQUIRED = "destructive_change_required"
    # Safety-net reasons — raised by the loop, not the agent itself:
    VERIFICATION_GATE_VIOLATION = "verification_gate_violation"
    IMPLICIT_STOP = "implicit_stop"


# ── Trace types (spec §8) ─────────────────────────────────────────────────
class TraceType(str, Enum):
    """Maps to AgentTrace.trace_type rows written per loop event."""

    REFLECTION = "reflection"
    DECISION = "decision"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    UNCERTAINTY = "uncertainty"
    HANDOFF = "handoff"
    SELF_CHECK = "self_check"


# ── Commit strategies (spec §1 + §4.3) ────────────────────────────────────
class CommitStrategy(str, Enum):
    """Selected by the agent based on has_write_permission.

    AUTHOR_BRANCH: commit directly to the author's PR branch (requires
    write permission).
    FIX_BRANCH: open a new PR whose base is the author's PR branch
    (fallback when write permission is not available).
    """

    AUTHOR_BRANCH = "author_branch"
    FIX_BRANCH = "fix_branch"


# ── Final run verdicts ─────────────────────────────────────────────────────
class RunVerdict(str, Enum):
    """Terminal state of a v2 run; written to CIFixRun.status."""

    COMMITTED = "committed"
    ESCALATED = "escalated"
    FAILED = "failed"
