"""
PhaseGenerator — Step 2 of PromptEnricher.

Takes the IntentDocument from Step 1 and asks GPT-4o to generate
N phases of Claude-ready execution prompts.

Each phase prompt is a complete, role-assigned, context-rich instruction
that Claude receives as the task description. This is where quality is made.

The output PhaseSpec[] is saved to WorkOrder.enriched_spec.phases.
"""
from __future__ import annotations

from typing import Any

import structlog

from phalanx.agents.openai_client import OpenAIClient

log = structlog.get_logger(__name__)

# The meta-prompt: tells GPT-4o how to write Claude execution prompts
_SYSTEM_PROMPT = """\
You are a Principal Engineering Manager at a FAANG-level company. You have shipped \
50+ products and you write the most precise, thorough technical execution prompts for \
AI coding agents.

You will receive an IntentDocument describing what a user wants to build.
Your job: generate N phase-by-phase execution prompts that AI coding agents (Claude) \
will execute to produce production-quality software.

CRITICAL: The quality of the output is 100% determined by the quality of the prompts \
you write. Vague prompts produce generic slop. Precise, role-assigned, context-rich \
prompts produce elite software.

For EACH phase, you MUST produce a complete PhaseSpec following this exact template:

{
  "id": <1-indexed integer>,
  "name": "<Phase Name>",
  "agent_role": "builder",
  "role": {
    "title": "<exact title: e.g. Sr UX Researcher and Product Designer>",
    "seniority": "<e.g. Senior | Staff | Principal>",
    "domain": "<specialization domain>",
    "persona": "<3-4 sentence backstory establishing deep credibility and specific expertise relevant to this phase. Reference real companies, real patterns, real tradeoffs.>"
  },
  "context": "<2-3 sentences describing what phase this is, what came before (if applicable), and what this phase establishes for future phases>",
  "objectives": ["<specific objective 1>", "<specific objective 2>", ...],
  "deliverables": [
    {"file": "<exact file path>", "description": "<what this file contains>"},
    ...
  ],
  "acceptance_criteria": ["<testable criterion 1>", "<testable criterion 2>", ...],
  "rules": {
    "do": ["<must-follow pattern 1>", ...],
    "dont": ["<forbidden approach 1>", ...]
  },
  "claude_prompt": "<THE FULL EXECUTION PROMPT — 400-800 words. This is what Claude receives as its task. Include the role block, context, objectives, deliverables, acceptance criteria, tech stack specs, and do/don't rules, all woven into a single authoritative instruction. Write it as if you are briefing a brilliant contractor who needs zero ambiguity to do excellent work.>"
}

Return a single JSON object:
{
  "phases": [<PhaseSpec>, <PhaseSpec>, ...]
}

PHASE STRUCTURE BY PRODUCT TYPE:

ios_app / android_app / mobile_app:
  Phase 1: UX Research & Information Architecture (role: Sr UX Researcher + Product Designer)
  Phase 2: Design System & Component Architecture (role: Sr Product Designer + iOS/Android specialist)
  Phase 3: App Shell & Navigation (role: Staff iOS/Android Engineer)
  Phase 4: Core Features Implementation (role: Sr iOS/Android Engineer)
  Phase 5 (if complex): Polish, Accessibility & App Store Prep (role: Sr iOS/Android Engineer)

web_app / full_stack:
  Phase 1: UX Research & Information Architecture (role: Sr UX Researcher + Product Designer)
  Phase 2: Design System & Component Library (role: Sr Frontend Engineer + Design Systems specialist)
  Phase 3: App Shell, Routing & Data Layer (role: Staff Full-Stack Engineer)
  Phase 4: Core Features (role: Sr Full-Stack Engineer)
  Phase 5 (if complex): Performance, SEO & Launch Prep (role: Sr Frontend Engineer)

api:
  Phase 1: Domain Modeling & Architecture (role: Staff Backend Engineer)
  Phase 2: Data Layer & Migrations (role: Sr Backend Engineer)
  Phase 3: Business Logic & Services (role: Sr Backend Engineer)
  Phase 4: API Layer, Auth & Security (role: Sr Backend Engineer + Security specialist)

cli:
  Phase 1: Command Design & UX (role: Sr Software Engineer)
  Phase 2: Core Implementation (role: Sr Software Engineer)
  Phase 3: Config, Packaging & Distribution (role: Sr DevOps + Software Engineer)

RULES FOR WRITING claude_prompt:
1. Open with [ROLE] block — title + persona, 2-3 sentences
2. [CONTEXT] block — product name, phase N of M, what was built before
3. [OBJECTIVES] — bullet list of 3-6 specific things this phase achieves
4. [DELIVERABLES] — exact file names with descriptions
5. [TECH STACK] — exact versions, patterns, forbidden libraries
6. [ACCEPTANCE CRITERIA] — specific, testable conditions
7. [RULES] — DO and DON'T bullets
8. Close with a clear execution instruction: "Implement all deliverables above. Write complete, production-ready code. Do not skip any file."

The claude_prompt must be self-contained — Claude has no other context except what's in this prompt plus the IntentDocument summary.
"""


class PhaseGenerator:
    """
    Calls GPT-4o to generate phase-by-phase Claude execution prompts.

    Returns a list of PhaseSpec dicts saved to WorkOrder.enriched_spec.
    """

    def __init__(self) -> None:
        self._client = OpenAIClient()
        self._log = log.bind(step="phase_generation")

    def generate(
        self,
        intent_doc: dict[str, Any],
        issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Generate phase prompts from a NormalizedSpec (intent_doc).

        Adjusts instructions based on execution_mode from the router:
          - mvp_build       → vague/semi-specified: expand carefully
          - full_spec_build → expert: preserve all stated details

        Args:
            intent_doc: Output from RequirementNormalizer (includes _execution_mode, _request_type)
            issues: Optional list of issues from a prior failed DryRunValidator run

        Returns:
            dict with key "phases": list of PhaseSpec dicts
        """
        execution_mode = intent_doc.get("_execution_mode", "mvp_build")

        self._log.info(
            "phase_generator.start",
            product_type=intent_doc.get("product_type"),
            total_phases=intent_doc.get("total_phases"),
            execution_mode=execution_mode,
            retry_with_issues=bool(issues),
        )

        import json  # noqa: PLC0415

        intent_summary = json.dumps(intent_doc, indent=2)

        # Execution-mode-aware instruction
        if execution_mode == "full_spec_build":
            mode_instruction = (
                "This is an EXPERT SPEC. The user has provided explicit requirements. "
                "Preserve every stated feature and technical decision exactly. "
                "Do NOT add features that are not in the spec. "
                "Do NOT suggest alternative tech stacks. "
                "Convert the explicit requirements directly into implementation phases."
            )
        else:
            mode_instruction = (
                "This is an MVP BUILD from a vague/semi-specified request. "
                "Expand carefully using safe defaults. "
                "Keep scope minimal — only must-have features in early phases. "
                "Nice-to-have features can be in later phases or omitted."
            )

        retry_block = ""
        if issues:
            retry_block = (
                "\n\nIMPORTANT — Previous generation failed validation. Fix these issues:\n"
                + "\n".join(f"  - {issue}" for issue in issues)
                + "\n"
            )

        messages = [
            {
                "role": "user",
                "content": (
                    f"NormalizedSpec:\n\n{intent_summary}\n\n"
                    f"{mode_instruction}\n"
                    f"{retry_block}"
                    "\nGenerate the complete phase-by-phase execution plan. "
                    "Each phase's claude_prompt must be detailed enough that Claude "
                    "can execute it perfectly without any additional context. "
                    "The quality of these prompts determines the quality of the final product."
                ),
            }
        ]

        result = self._client.call(
            messages=messages,
            system=_SYSTEM_PROMPT,
            max_tokens=8192,  # Phases need room — each claude_prompt is 400-800 words
            temperature=0.2 if execution_mode == "full_spec_build" else 0.3,
        )

        phases = result.get("phases", [])
        self._log.info(
            "phase_generator.done",
            phases_count=len(phases),
            phase_names=[p.get("name", "?") for p in phases],
        )

        return result
