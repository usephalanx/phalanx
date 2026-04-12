"""
IntentRouter — Stage 0 of the Phalanx front-door pipeline.

Analyzes the raw user request and produces a classified intent package
for the RequirementNormalizer (Stage 1).

Design principle: preserve what the user said, infer only what is necessary,
label every assumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from phalanx.agents.openai_client import OpenAIClient

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are the Intent Router for Phalanx.

Your job is to analyze an incoming user request and prepare it for downstream execution.

You must work for:
- vague beginner requests
- semi-specified requests
- expert technical/product specs
- follow-up / continuation requests
- mixed requests containing multiple intents

Your responsibilities:
1. Preserve explicit user intent exactly.
2. Detect the request type.
3. Detect whether the message contains multiple distinct intents.
4. Separate explicit requirements from assumptions.
5. Avoid hallucinating missing business logic.
6. Decide whether the request is ready for normalization/planning.

Rules:
- Do NOT invent product requirements that the user did not state.
- Do NOT rewrite away important technical details.
- If the user already provided a detailed spec, preserve it as source of truth.
- If the user provided multiple distinct asks, split them clearly.
- If details are missing, note them as unknowns instead of guessing.
- If the request appears to be a continuation of previous work, mark it clearly.
- Prefer safe defaults only when they do not materially change the product.
- Be concise and structured.

Return valid JSON only.

JSON schema:
{
  "request_type": "vague_request | semi_specified_request | expert_spec | mixed_multi_intent | continuation_request",
  "primary_intent": {
    "summary": "string",
    "category": "web_app | mobile_app | backend_api | website | script_tool | workflow_automation | research | debugging | refactor | docs | unknown"
  },
  "secondary_intents": [
    {
      "summary": "string",
      "category": "web_app | mobile_app | backend_api | website | script_tool | workflow_automation | research | debugging | refactor | docs | unknown"
    }
  ],
  "explicit_requirements": ["string"],
  "explicit_constraints": ["string"],
  "inferred_assumptions": ["string"],
  "unknowns": ["string"],
  "execution_readiness": "ready_for_normalization | needs_light_defaults | needs_intent_split | needs_human_clarification",
  "risk_flags": ["ambiguous_scope | mixed_intents | missing_core_requirements | conflicting_requirements | none"],
  "recommended_next_step": "string"
}"""

_AUTO_PROCEED_READINESS = {"ready_for_normalization", "needs_light_defaults"}


@dataclass
class RouterResult:
    """Structured output from IntentRouter."""

    request_type: str
    primary_intent: dict[str, str]  # {summary, category}
    secondary_intents: list[dict[str, str]]  # [{summary, category}]
    explicit_requirements: list[str]
    explicit_constraints: list[str]
    inferred_assumptions: list[str]
    unknowns: list[str]
    execution_readiness: str
    risk_flags: list[str]
    recommended_next_step: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def can_auto_proceed(self) -> bool:
        return self.execution_readiness in _AUTO_PROCEED_READINESS

    @property
    def needs_split(self) -> bool:
        return (
            self.request_type == "mixed_multi_intent"
            or self.execution_readiness == "needs_intent_split"
        )

    @property
    def is_expert_spec(self) -> bool:
        return self.request_type == "expert_spec"

    def to_context_block(self) -> str:
        """Compact summary injected into downstream stage prompts."""
        lines = [
            "[ROUTER OUTPUT]",
            f"request_type: {self.request_type}",
            f"primary_intent: {self.primary_intent.get('summary', '')} "
            f"[{self.primary_intent.get('category', '')}]",
            f"execution_readiness: {self.execution_readiness}",
        ]
        if self.explicit_requirements:
            lines.append("explicit_requirements:")
            lines.extend(f"  - {r}" for r in self.explicit_requirements)
        if self.explicit_constraints:
            lines.append("explicit_constraints:")
            lines.extend(f"  - {c}" for c in self.explicit_constraints)
        if self.inferred_assumptions:
            lines.append("inferred_assumptions:")
            lines.extend(f"  - {a}" for a in self.inferred_assumptions)
        if self.unknowns:
            lines.append("unknowns:")
            lines.extend(f"  - {u}" for u in self.unknowns)
        if self.risk_flags and self.risk_flags != ["none"]:
            lines.append(f"risk_flags: {', '.join(self.risk_flags)}")
        return "\n".join(lines)


class IntentRouter:
    """
    Stage 0 — classifies and preserves the raw user request.
    Output feeds directly into RequirementNormalizer as router_output.
    """

    def __init__(self) -> None:
        self._client = OpenAIClient()
        self._log = log.bind(step="intent_routing")

    def route(self, raw_prompt: str) -> RouterResult:
        self._log.info("intent_router.start", prompt_len=len(raw_prompt))

        raw = self._client.call(
            messages=[{"role": "user", "content": raw_prompt}],
            system=_SYSTEM_PROMPT,
            max_tokens=1024,
            temperature=0.1,
        )

        primary = raw.get("primary_intent", {})
        if isinstance(primary, str):
            primary = {"summary": primary, "category": "unknown"}

        result = RouterResult(
            request_type=raw.get("request_type", "vague_request"),
            primary_intent=primary,
            secondary_intents=raw.get("secondary_intents", []),
            explicit_requirements=raw.get("explicit_requirements", []),
            explicit_constraints=raw.get("explicit_constraints", []),
            inferred_assumptions=raw.get("inferred_assumptions", []),
            unknowns=raw.get("unknowns", []),
            execution_readiness=raw.get("execution_readiness", "needs_light_defaults"),
            risk_flags=raw.get("risk_flags", ["none"]),
            recommended_next_step=raw.get("recommended_next_step", ""),
            raw=raw,
        )

        self._log.info(
            "intent_router.done",
            request_type=result.request_type,
            category=result.primary_intent.get("category"),
            execution_readiness=result.execution_readiness,
            can_auto_proceed=result.can_auto_proceed,
            explicit_count=len(result.explicit_requirements),
            unknowns_count=len(result.unknowns),
            risk_flags=result.risk_flags,
        )

        return result
