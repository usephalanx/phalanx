"""
DryRunValidator — Stage 3 of the PromptEnricher pipeline.

Validates the ExecutionPlan against the NormalizedSpec with structured findings.

Contract:
  status: pass | revise | block
  - pass   → proceed to Commander
  - revise → retry ExecutionPlanner (Stage 2) with specific fixable issues
  - block  → structural problem, cannot be fixed by replanning — escalate

Issue categories (each tagged so the pipeline knows how to route):
  missing_explicit_requirement   → fixable (revise)
  expanded_beyond_scope          → fixable (revise)
  conflicting_tasks              → fixable (revise)
  no_verification_plan           → fixable (revise)
  acceptance_criteria_too_vague  → fixable (revise)
  unsafe_repo_action             → fixable (revise)
  mixed_intent_unresolved        → structural (block)
  incoherent_phase_order         → structural (block)
  missing_core_requirement       → structural (block) if unresolvable

A single score hiding the reason for failure is replaced with:
  confidence: 0-100  (how confident the validator is in its assessment)
  findings: [{ type, severity, description, fixable }]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from phalanx.agents.openai_client import OpenAIClient

log = structlog.get_logger(__name__)

# Issue types that can be fixed by replanning (retry Stage 2)
_REVISE_TYPES = {
    "missing_explicit_requirement",
    "expanded_beyond_scope",
    "conflicting_tasks",
    "no_verification_plan",
    "acceptance_criteria_too_vague",
    "unsafe_repo_action",
}

# Issue types that indicate a structural problem (escalate, don't retry)
_BLOCK_TYPES = {
    "mixed_intent_unresolved",
    "incoherent_phase_order",
    "missing_core_requirement",
}

_SYSTEM_PROMPT = """\
You are a Staff Engineering Manager reviewing an AI-generated execution plan for a software build pipeline.

You will receive:
  1. A NormalizedSpec — what the user wants built (explicit requirements, scope, constraints)
  2. An ExecutionPlan — the phases and tasks the planner produced

Your job: audit the plan against the spec and return structured findings.

AUDIT CHECKLIST:
  A. COVERAGE       — Does every explicit requirement in NormalizedSpec appear in at least one task?
  B. SCOPE          — Did the planner add anything NOT in NormalizedSpec.mvp_scope.in_scope?
  C. CONFLICTS      — Are there any tasks that contradict each other or create ordering problems?
  D. VERIFICATION   — Is there a verification_plan with at least one build or test check?
  E. CRITERIA       — Are acceptance_criteria specific and testable (not "it works", not empty)?
  F. REPO SAFETY    — Are repo_actions safe? (no force-push to main, no branch deletions)
  G. INTENT         — If mixed intents were detected, are they properly separated or flagged?
  H. COHERENCE      — Are phases in a logical incremental order?

Return valid JSON only:
{
  "status": "pass | revise | block",
  "confidence": <0-100 integer>,
  "findings": [
    {
      "type": "missing_explicit_requirement | expanded_beyond_scope | conflicting_tasks | no_verification_plan | acceptance_criteria_too_vague | unsafe_repo_action | mixed_intent_unresolved | incoherent_phase_order | missing_core_requirement",
      "severity": "critical | major | minor",
      "description": "precise description of the problem",
      "fixable": true | false,
      "suggested_fix": "one sentence on how to fix it (if fixable)"
    }
  ],
  "revise_instructions": ["specific instruction for the planner on what to fix"],
  "task_findings": [
    {"task_id": "string", "issue": "string", "severity": "critical | major | minor"}
  ],
  "summary": "one sentence overall assessment"
}

STATUS RULES:
- pass  → no critical findings AND no major findings of type missing_explicit_requirement or expanded_beyond_scope
- block → any finding with type mixed_intent_unresolved OR incoherent_phase_order OR (missing_core_requirement AND fixable=false)
- revise → everything else that is not pass or block

confidence should reflect how certain you are of your assessment (not the plan quality).
Be precise in descriptions — vague findings like "plan is incomplete" are not acceptable.
"""


@dataclass
class Finding:
    """A single structured finding from the validator."""

    type: str
    severity: str  # critical | major | minor
    description: str
    fixable: bool
    suggested_fix: str


@dataclass
class ValidationResult:
    """
    Structured result of a dry-run validation.

    status:
      pass   → proceed
      revise → retry ExecutionPlanner with revise_instructions
      block  → structural problem, escalate to human
    """

    status: str  # pass | revise | block
    confidence: int  # 0-100
    findings: list[Finding]
    revise_instructions: list[str]  # fed back to ExecutionPlanner on retry
    task_findings: list[dict]
    summary: str
    raw: dict[str, Any] = field(default_factory=dict)

    # ── Backwards-compatible properties ──────────────────────────────────────
    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def score(self) -> int:
        return self.confidence

    @property
    def issues(self) -> list[str]:
        """Flat issue list for retry — includes revise_instructions + critical findings."""
        return self.revise_instructions or [
            f.description for f in self.findings if f.severity == "critical"
        ]

    @property
    def suggestions(self) -> list[str]:
        return [f.suggested_fix for f in self.findings if f.fixable and f.suggested_fix]

    @property
    def is_blocked(self) -> bool:
        return self.status == "block"

    @property
    def critical_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def fixable_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.fixable]


class DryRunValidator:
    """
    Validates ExecutionPlan against NormalizedSpec with structured findings.

    Returns ValidationResult with status: pass | revise | block.
    Callers should:
      - pass   → proceed to Commander
      - revise → retry ExecutionPlanner with result.revise_instructions
      - block  → surface to human, do not retry
    """

    def __init__(self) -> None:
        self._client = OpenAIClient()
        self._log = log.bind(step="dry_run_validation")

    def validate(
        self,
        intent_doc: dict[str, Any],
        execution_plan: dict[str, Any],
    ) -> ValidationResult:
        """
        Validate the execution plan against the normalized spec.

        Args:
            intent_doc:     NormalizedSpec dict (WorkOrder.intent)
            execution_plan: ExecutionPlan dict (from ExecutionPlanner.to_enriched_spec())

        Returns:
            ValidationResult with status, findings, and revise_instructions.
        """
        phases = execution_plan.get("phases", [])
        self._log.info("dry_run_validator.start", phases_count=len(phases))

        messages = [
            {
                "role": "user",
                "content": (
                    f"NormalizedSpec:\n{json.dumps(intent_doc, indent=2)}\n\n"
                    f"ExecutionPlan ({len(phases)} phases/tasks):\n"
                    f"{json.dumps(execution_plan, indent=2)}\n\n"
                    "Audit the plan against the spec. Be precise — vague findings are not acceptable."
                ),
            }
        ]

        raw = self._client.call(
            messages=messages,
            system=_SYSTEM_PROMPT,
            max_tokens=2048,
            temperature=0.1,
        )

        findings = [
            Finding(
                type=f.get("type", "missing_explicit_requirement"),
                severity=f.get("severity", "major"),
                description=f.get("description", ""),
                fixable=f.get("fixable", True),
                suggested_fix=f.get("suggested_fix", ""),
            )
            for f in raw.get("findings", [])
        ]

        # Enforce status logic from issue types regardless of GPT classification
        status = raw.get("status", "revise")
        finding_types = {f.type for f in findings}
        if finding_types & _BLOCK_TYPES:
            # Any blocking issue type overrides GPT's status
            non_fixable_block = any(f.type in _BLOCK_TYPES and not f.fixable for f in findings)
            if non_fixable_block:
                status = "block"
        if not findings:
            status = "pass"

        result = ValidationResult(
            status=status,
            confidence=raw.get("confidence", 0),
            findings=findings,
            revise_instructions=raw.get("revise_instructions", []),
            task_findings=raw.get("task_findings", []),
            summary=raw.get("summary", ""),
            raw=raw,
        )

        self._log.info(
            "dry_run_validator.done",
            status=result.status,
            confidence=result.confidence,
            findings_count=len(result.findings),
            critical_count=len(result.critical_findings),
            is_blocked=result.is_blocked,
            finding_types=list(finding_types),
        )

        return result
