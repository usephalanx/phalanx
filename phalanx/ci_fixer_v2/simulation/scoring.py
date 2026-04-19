"""Per-fixture scoring — Strict / Lenient / Behavioral tiers.

Spec §12 pins the scoring semantics:

  - Strict:     agent's diff ~= author's actual resolution (similarity
                >= SIMILARITY_THRESHOLD). Informational only.
  - Lenient:    sandbox verification of the ORIGINAL failing command
                passed after the agent ran. GATING for MVP exit.
  - Behavioral: agent reached the correct decision class for this
                failure type (patch / mark_flaky / decline_as_preexisting
                / escalate). GATING for MVP exit.

Each score is derived from:
  - the Fixture (ground_truth / raw_log / meta)
  - the RunOutcome returned by run_ci_fix_v2
  - the AgentContext after the loop finishes (holds the diff + flags)

The scoring module is PURE — no I/O. Callers feed it the triple and
get back a FixtureScore dataclass suitable for aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from phalanx.ci_fixer_v2.agent import RunOutcome
from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.simulation.fixtures import Fixture


SIMILARITY_THRESHOLD: float = 0.85


# ── Decision classes (string-enum) ────────────────────────────────────────
# Ground-truth fix_type values appear here verbatim; the predictor maps
# RunOutcome + AgentContext into the same space so comparison is 1:1.
DECISION_CODE_CHANGE: str = "code_change"
DECISION_MARK_FLAKY: str = "mark_flaky_with_todo"
DECISION_RERUN: str = "rerun"
DECISION_DECLINE_PREEXISTING: str = "decline_as_preexisting"
DECISION_DECLINE_INFRA: str = "decline_as_infra"
DECISION_ESCALATE: str = "escalate"
DECISION_FAILED: str = "failed"


@dataclass
class FixtureScore:
    """Result of scoring one fixture run."""

    fixture_id: str
    language: str
    failure_class: str

    # Three score tiers (spec §12).
    strict: bool
    lenient: bool
    behavioral: bool

    # Supporting metrics.
    strict_similarity: float
    decision_class_predicted: str
    decision_class_expected: str

    # Secondary metrics tracked for the scoreboard.
    turns_used: int
    total_cost_usd: float
    escalation_reason: str | None = None
    notes: str = ""
    verdict: str = ""


def score_fixture(
    fixture: Fixture, outcome: RunOutcome, ctx: AgentContext
) -> FixtureScore:
    """Compute a FixtureScore for one agent run against one fixture."""
    expected_decision = _extract_expected_decision(fixture)
    predicted_decision = _predict_decision_class(outcome, ctx)

    strict_sim = _strict_similarity(fixture, ctx)
    strict_pass = strict_sim >= SIMILARITY_THRESHOLD

    # Spec §12: lenient = "original failing command passes in sandbox
    # after the agent ran". COMMITTED implies the verification gate
    # fired before push, so a COMMITTED verdict counts as lenient-pass
    # even though commit_and_push resets the flag on success (to force
    # re-verification before any follow-up commit). For non-committed
    # paths, fall back to the live flag.
    lenient_pass = (
        outcome.verdict == RunVerdict.COMMITTED
        or bool(ctx.last_sandbox_verified)
    )
    # Some fixtures are correctly resolved WITHOUT sandbox verification
    # (e.g. decline_as_preexisting). Accept lenient when the predicted
    # decision matches a non-code-change expected decision.
    if not lenient_pass and predicted_decision == expected_decision and predicted_decision in _NONCODE_DECISIONS:
        lenient_pass = True

    behavioral_pass = (
        predicted_decision == expected_decision and expected_decision != ""
    )

    return FixtureScore(
        fixture_id=fixture.fixture_id,
        language=fixture.meta.language,
        failure_class=fixture.meta.failure_class,
        strict=strict_pass,
        lenient=lenient_pass,
        behavioral=behavioral_pass,
        strict_similarity=round(strict_sim, 3),
        decision_class_predicted=predicted_decision,
        decision_class_expected=expected_decision,
        turns_used=len(ctx.tool_invocations),
        total_cost_usd=round(ctx.cost.total_cost_usd, 4),
        escalation_reason=(
            outcome.escalation_reason.value
            if outcome.escalation_reason
            else None
        ),
        verdict=outcome.verdict.value,
    )


# ── Internals ─────────────────────────────────────────────────────────────


_NONCODE_DECISIONS: frozenset[str] = frozenset(
    {DECISION_RERUN, DECISION_DECLINE_PREEXISTING, DECISION_DECLINE_INFRA}
)


def _extract_expected_decision(fixture: Fixture) -> str:
    gt = fixture.ground_truth or {}
    fix_type = gt.get("fix_type")
    if isinstance(fix_type, str) and fix_type:
        return fix_type
    return ""


def _predict_decision_class(outcome: RunOutcome, ctx: AgentContext) -> str:
    """Map (RunOutcome, AgentContext) to a string decision class."""
    if outcome.verdict == RunVerdict.COMMITTED:
        # Tell mark_flaky_with_todo from regular code changes by looking
        # at the diff we committed. Flake-suppression diffs canonically
        # introduce a `@pytest.mark.flaky` / `@flaky` marker or add an
        # `xfail` / `skip` with a TODO tagging the incident.
        diff = (ctx.last_attempted_diff or "").lower()
        if _looks_like_flaky_marker(diff):
            return DECISION_MARK_FLAKY
        return DECISION_CODE_CHANGE

    if outcome.verdict == RunVerdict.ESCALATED:
        reason = outcome.escalation_reason
        if reason == EscalationReason.PREEXISTING_MAIN_FAILURE:
            return DECISION_DECLINE_PREEXISTING
        if reason == EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE:
            return DECISION_DECLINE_INFRA
        return DECISION_ESCALATE

    return DECISION_FAILED


_FLAKY_MARKER_NEEDLES: tuple[str, ...] = (
    "mark.flaky",
    "@flaky",
    "pytest.mark.flaky",
    "pytest.mark.xfail",
    "pytest.mark.skip",
    "rerun_failures",
)


def _looks_like_flaky_marker(diff_lower: str) -> bool:
    if "todo" not in diff_lower:
        # Flake suppression without a TODO is forbidden per the system prompt;
        # treat a marker without TODO as a regular code change.
        return any(n in diff_lower for n in _FLAKY_MARKER_NEEDLES) and False
    return any(n in diff_lower for n in _FLAKY_MARKER_NEEDLES)


def _strict_similarity(fixture: Fixture, ctx: AgentContext) -> float:
    """Jaccard over non-empty, trimmed lines of the two diffs.

    Both diffs are stripped of `+` / `-` / `@@` hunk-header lines so we
    measure similarity of the actual content changes rather than of the
    diff framing. Returns 0.0 when either side is empty.
    """
    expected = ((fixture.ground_truth or {}).get("fix_diff") or "")
    actual = ctx.last_attempted_diff or ""
    a = _diff_content_lines(actual)
    b = _diff_content_lines(expected)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _diff_content_lines(diff: str) -> set[str]:
    """Return the meaningful (+/- prefixed) content lines of a diff."""
    out: set[str] = set()
    for line in diff.splitlines():
        stripped = line.rstrip("\n")
        if not stripped:
            continue
        if stripped.startswith("diff --git") or stripped.startswith("index "):
            continue
        if stripped.startswith("--- ") or stripped.startswith("+++ "):
            continue
        if stripped.startswith("@@"):
            continue
        if stripped[:1] in ("+", "-", " "):
            content = stripped[1:].strip()
            if content:
                out.add(content)
    return out
