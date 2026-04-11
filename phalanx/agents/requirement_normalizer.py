"""
RequirementNormalizer — Stage 1 of the Phalanx front-door pipeline.

Takes the RouterResult from Stage 0 and converts it into a structured,
implementation-ready requirement package.

Design principle: preserve explicit requirements, add only minimal safe defaults,
clearly separate explicit / assumptions / unknowns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from phalanx.agents.intent_router import RouterResult
from phalanx.agents.openai_client import OpenAIClient

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are the Requirement Normalizer for Phalanx.

Your job is to convert a routed user intent into a structured, implementation-ready requirement package.

You must preserve explicit requirements and constraints.
You may add only minimal, safe defaults when required for execution.
You must clearly separate:
- explicit requirements
- assumptions
- unresolved unknowns
- execution defaults

Rules:
- Do NOT hallucinate advanced features or business logic.
- Do NOT add backend, auth, database, payments, or integrations unless explicitly requested or clearly required.
- If the request is broad, reduce it to an MVP that still satisfies the user's stated goal.
- If the user supplied a detailed expert spec, preserve that detail faithfully.
- If something is unclear and not blocking, choose a safe default and label it.
- If something is blocking, mark it as unresolved.
- Be implementation-oriented, not aspirational.

Return valid JSON only.

JSON schema:
{
  "normalized_goal": "string",
  "artifact_type": "website | web_app | mobile_app | backend_api | script_tool | workflow | doc | unknown",
  "execution_mode": "demo_only | prototype | mvp | production_like",
  "target_users": ["string"],
  "core_user_problem": "string",
  "success_criteria": ["string"],
  "mvp_scope": {
    "in_scope": ["string"],
    "out_of_scope": ["string"]
  },
  "functional_requirements": ["string"],
  "non_functional_requirements": ["string"],
  "technical_constraints": ["string"],
  "design_requirements": ["string"],
  "content_requirements": ["string"],
  "safe_defaults": ["string"],
  "assumptions": ["string"],
  "unresolved_unknowns": ["string"],
  "delivery_expectations": {
    "should_create_branch": true,
    "should_run_build": true,
    "should_run_tests": true,
    "should_open_pr": true
  }
}"""


@dataclass
class NormalizedSpec:
    """Structured requirement package from RequirementNormalizer."""

    normalized_goal: str
    artifact_type: str
    execution_mode: str
    target_users: list[str]
    core_user_problem: str
    success_criteria: list[str]
    mvp_scope: dict[str, list[str]]          # {in_scope: [], out_of_scope: []}
    functional_requirements: list[str]
    non_functional_requirements: list[str]
    technical_constraints: list[str]
    design_requirements: list[str]
    content_requirements: list[str]
    safe_defaults: list[str]
    assumptions: list[str]
    unresolved_unknowns: list[str]
    delivery_expectations: dict[str, bool]
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return as plain dict for DB storage and downstream stage input."""
        return {
            "normalized_goal": self.normalized_goal,
            "artifact_type": self.artifact_type,
            "execution_mode": self.execution_mode,
            "target_users": self.target_users,
            "core_user_problem": self.core_user_problem,
            "success_criteria": self.success_criteria,
            "mvp_scope": self.mvp_scope,
            "functional_requirements": self.functional_requirements,
            "non_functional_requirements": self.non_functional_requirements,
            "technical_constraints": self.technical_constraints,
            "design_requirements": self.design_requirements,
            "content_requirements": self.content_requirements,
            "safe_defaults": self.safe_defaults,
            "assumptions": self.assumptions,
            "unresolved_unknowns": self.unresolved_unknowns,
            "delivery_expectations": self.delivery_expectations,
        }


class RequirementNormalizer:
    """
    Stage 1 — converts RouterResult into a structured NormalizedSpec.

    Passes the full router_output JSON as input so the model sees exactly
    what was explicit vs inferred vs unknown.
    """

    def __init__(self) -> None:
        self._client = OpenAIClient()
        self._log = log.bind(step="requirement_normalization")

    def normalize(self, router_result: RouterResult) -> NormalizedSpec:
        """
        Normalize a routed request into a structured requirement package.

        Args:
            router_result: Output from IntentRouter (Stage 0).

        Returns:
            NormalizedSpec ready for the Planner (Stage 2).
        """
        self._log.info(
            "requirement_normalizer.start",
            request_type=router_result.request_type,
            execution_readiness=router_result.execution_readiness,
        )

        user_content = json.dumps({"router_output": router_result.raw}, indent=2)

        raw = self._client.call(
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM_PROMPT,
            max_tokens=2048,
            # Expert specs get lower temp: preserve fidelity
            # Vague requests get slightly higher: safe creative defaults OK
            temperature=0.1 if router_result.is_expert_spec else 0.2,
        )

        delivery = raw.get("delivery_expectations", {})

        result = NormalizedSpec(
            normalized_goal=raw.get("normalized_goal", ""),
            artifact_type=raw.get("artifact_type", "unknown"),
            execution_mode=raw.get("execution_mode", "mvp"),
            target_users=raw.get("target_users", []),
            core_user_problem=raw.get("core_user_problem", ""),
            success_criteria=raw.get("success_criteria", []),
            mvp_scope=raw.get("mvp_scope", {"in_scope": [], "out_of_scope": []}),
            functional_requirements=raw.get("functional_requirements", []),
            non_functional_requirements=raw.get("non_functional_requirements", []),
            technical_constraints=raw.get("technical_constraints", []),
            design_requirements=raw.get("design_requirements", []),
            content_requirements=raw.get("content_requirements", []),
            safe_defaults=raw.get("safe_defaults", []),
            assumptions=raw.get("assumptions", []),
            unresolved_unknowns=raw.get("unresolved_unknowns", []),
            delivery_expectations={
                "should_create_branch": delivery.get("should_create_branch", True),
                "should_run_build": delivery.get("should_run_build", True),
                "should_run_tests": delivery.get("should_run_tests", True),
                "should_open_pr": delivery.get("should_open_pr", True),
            },
            raw=raw,
        )

        self._log.info(
            "requirement_normalizer.done",
            artifact_type=result.artifact_type,
            execution_mode=result.execution_mode,
            functional_reqs=len(result.functional_requirements),
            in_scope=len(result.mvp_scope.get("in_scope", [])),
            out_of_scope=len(result.mvp_scope.get("out_of_scope", [])),
            unknowns=len(result.unresolved_unknowns),
        )

        return result
