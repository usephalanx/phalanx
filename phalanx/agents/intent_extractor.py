"""
IntentExtractor — Step 1 of PromptEnricher.

Takes a raw user prompt ("build an ios app for photoshoot promotions")
and asks GPT-4o to deeply understand the intent, context, users, tech stack,
implicit requirements, and success definition.

The output IntentDocument is immutable once created — it travels with every
agent (builder, reviewer, QA) for the entire lifetime of the WorkOrder.
"""
from __future__ import annotations

from typing import Any

import structlog

from phalanx.agents.openai_client import OpenAIClient

log = structlog.get_logger(__name__)

# System prompt that turns GPT-4o into a principal architect who extracts intent
_SYSTEM_PROMPT = """\
You are a Principal Engineering Manager and Product Architect at a top-tier tech company \
(think Google, Stripe, Figma). You have shipped 50+ products across iOS, web, API, and \
full-stack domains.

Your job: extract the FULL product intent from a raw user prompt, no matter how vague.
You MUST infer what the user hasn't said but clearly needs. Think like a senior PM doing \
a discovery session — uncover the real problem, the real users, and the real success criteria.

You MUST return a single JSON object with EXACTLY this structure:

{
  "product_type": "one of: ios_app | android_app | mobile_app | web_app | api | cli | full_stack | desktop_app | chrome_extension | other",
  "product_name": "inferred product name (2-4 words, title case)",
  "tagline": "one-line value proposition (< 10 words)",
  "platform": "specific platform string e.g. iOS 17+, React web, Node.js API",
  "target_users": [
    {
      "persona": "persona name",
      "description": "who they are and what they need from this product"
    }
  ],
  "core_problem": "one sentence — the exact pain point this product solves",
  "success_definition": "one sentence — what 'done' looks like from the user's perspective",
  "key_features": ["feature 1", "feature 2", ...],
  "implicit_requirements": [
    "requirement 1 — things the user didn't say but clearly needs",
    "requirement 2",
    ...
  ],
  "tech_stack": {
    "language": "primary language",
    "ui_framework": "UI framework or empty string",
    "architecture": "architecture pattern (MVVM, MVC, Clean, etc.)",
    "backend": "backend tech or 'none' for pure frontend",
    "auth": "authentication approach",
    "storage": "data storage approach",
    "key_libraries": ["lib1", "lib2"],
    "minimum_version": "minimum platform version e.g. iOS 17.0, Node 20"
  },
  "constraints": ["constraint 1", "constraint 2"],
  "non_goals": ["thing NOT in scope 1", "thing NOT in scope 2"],
  "complexity": "one of: low | medium | medium_high | high | very_high",
  "total_phases": 3,
  "phase_overview": [
    "Phase 1: UX Research & Information Architecture",
    "Phase 2: Core Implementation",
    "Phase 3: Polish & Ship"
  ]
}

Rules:
- NEVER say you can't understand the prompt — always infer intelligently
- total_phases should be 3 for simple products, 4 for medium, 5 for complex
- implicit_requirements must include: auth, notifications, error states, empty states, loading states (for UI products)
- tech_stack must be specific — no "TBD" or "depends", make a decision
- Be opinionated: choose the best tech stack for the product type
- For iOS: Swift 5.9+, SwiftUI, MVVM-C
- For web: Next.js 14 App Router, TypeScript, Tailwind CSS
- For API: FastAPI or Node/Express, depending on complexity
- For full-stack: Next.js 14 + PostgreSQL + Prisma
"""


class IntentExtractor:
    """
    Calls GPT-4o to extract the full product intent from a raw user prompt.

    Returns an IntentDocument dict that is saved to WorkOrder.intent.
    """

    def __init__(self) -> None:
        self._client = OpenAIClient()
        self._log = log.bind(step="intent_extraction")

    def extract(self, raw_prompt: str) -> dict[str, Any]:
        """
        Extract intent from a raw user prompt.

        Args:
            raw_prompt: The user's raw input, e.g. "build an ios app for photoshoot promotions"

        Returns:
            IntentDocument dict with product_type, target_users, tech_stack, phases, etc.
        """
        self._log.info("intent_extractor.start", prompt_len=len(raw_prompt))

        messages = [
            {
                "role": "user",
                "content": (
                    f"User's raw prompt:\n\n\"{raw_prompt}\"\n\n"
                    "Extract the full product intent. Be thorough, specific, and opinionated. "
                    "Infer everything that a senior engineer would need to know to build this product."
                ),
            }
        ]

        result = self._client.call(
            messages=messages,
            system=_SYSTEM_PROMPT,
            max_tokens=2048,
            temperature=0.2,  # Low temp for consistent, precise extraction
        )

        self._log.info(
            "intent_extractor.done",
            product_type=result.get("product_type"),
            product_name=result.get("product_name"),
            total_phases=result.get("total_phases"),
            features_count=len(result.get("key_features", [])),
            implicit_count=len(result.get("implicit_requirements", [])),
        )

        return result
