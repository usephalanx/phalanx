"""
PromptEnricher — transforms a raw user prompt into a phase-by-phase execution plan.

3-stage front-door pipeline:
  Stage 0 — IntentRouter:            classify, preserve, detect mixed intents
  Stage 1 — RequirementNormalizer:   structure the spec, safe defaults only
  Stage 2 — ExecutionPlanner:        phases + tasks + acceptance criteria
  Stage 3 — DryRunValidator:         validate coverage + coherence (retry loop)

Design principle:
  Preserve what the user said. Infer only what is necessary. Label every assumption.
  The more explicit the user is, the less the model should invent.

After enrichment:
  - WorkOrder.intent        = NormalizedSpec  (immutable, from Stage 1)
  - WorkOrder.enriched_spec = ExecutionPlan   (immutable, from Stage 2)
  - WorkOrder.current_phase = 1               (ready for Phase 1 run)

Commander reads enriched_spec.phases[] to create DB tasks.
Builder reads task.role_context to adopt the right expert persona.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

_MAX_PLAN_RETRIES = 2  # Max retries when DryRunValidator fails


@dataclass
class EnrichmentResult:
    """Result of the full enrichment pipeline."""

    success: bool
    intent_doc: dict[str, Any]        # NormalizedSpec dict (WorkOrder.intent)
    enriched_spec: dict[str, Any]     # ExecutionPlan dict  (WorkOrder.enriched_spec)
    validation_score: int             # validator confidence 0-100
    phases_count: int
    request_type: str = "vague_request"
    validation_status: str = "pass"   # pass | revise | block
    validation_findings: list = field(default_factory=list)  # Finding descriptions for DB
    error: str | None = None


class PromptEnricher:
    """
    Orchestrates the 3-stage enrichment pipeline for a WorkOrder.

    Usage:
        enricher = PromptEnricher(work_order_id="...", project_id="...")
        result = enricher.run(raw_prompt="build a mobile app for photoshoots")
        await enricher.persist(result)
    """

    def __init__(self, work_order_id: str, project_id: str) -> None:
        self.work_order_id = work_order_id
        self.project_id = project_id
        self._log = log.bind(
            work_order_id=work_order_id[:8],
            component="prompt_enricher",
        )

    def run(
        self,
        raw_prompt: str,
        context: Any | None = None,   # ContextPackage from ContextResolver
    ) -> EnrichmentResult:
        """
        Run the full 3-stage pipeline synchronously.

        Args:
            raw_prompt: Raw user input from Slack/Discord.
            context:    ContextPackage from ContextResolver (optional).
                        When provided, injected into IntentRouter so it can
                        detect continuation_request with full prior-work context.

        Returns EnrichmentResult — caller calls persist() to write to DB.
        """
        self._log.info(
            "prompt_enricher.start",
            prompt_len=len(raw_prompt),
            context_type=getattr(context, "context_type", "none"),
        )

        from phalanx.agents.dry_run_validator import DryRunValidator        # noqa: PLC0415
        from phalanx.agents.execution_planner import ExecutionPlanner       # noqa: PLC0415
        from phalanx.agents.intent_router import IntentRouter               # noqa: PLC0415
        from phalanx.agents.requirement_normalizer import RequirementNormalizer  # noqa: PLC0415

        # ── Stage 0: Route ────────────────────────────────────────────────────
        self._log.info("prompt_enricher.stage0_route")
        try:
            # Build augmented prompt if we have prior context
            prompt_with_context = raw_prompt
            if context and context.has_prior_work:
                prompt_with_context = (
                    f"{context.to_context_block()}\n\n"
                    f"New request: {raw_prompt}"
                )
            router_result = IntentRouter().route(prompt_with_context)
        except Exception as exc:
            self._log.error("prompt_enricher.route_failed", error=str(exc))
            return EnrichmentResult(
                success=False, intent_doc={}, enriched_spec={},
                validation_score=0, phases_count=0,
                error=f"Intent routing failed: {exc}",
            )

        self._log.info(
            "prompt_enricher.routed",
            request_type=router_result.request_type,
            execution_readiness=router_result.execution_readiness,
            can_auto_proceed=router_result.can_auto_proceed,
            risk_flags=router_result.risk_flags,
        )

        # ── Stage 1: Normalize ────────────────────────────────────────────────
        self._log.info(
            "prompt_enricher.stage1_normalize",
            request_type=router_result.request_type,
        )
        try:
            normalized = RequirementNormalizer().normalize(router_result)
            # intent_doc stored in WorkOrder.intent (immutable for lifetime of WO)
            intent_doc = normalized.to_dict()
            intent_doc["_request_type"] = router_result.request_type
            intent_doc["_execution_readiness"] = router_result.execution_readiness
        except Exception as exc:
            self._log.error("prompt_enricher.normalize_failed", error=str(exc))
            return EnrichmentResult(
                success=False, intent_doc={}, enriched_spec={},
                validation_score=0, phases_count=0,
                error=f"Requirement normalization failed: {exc}",
            )

        # ── Stage 2 + 3: Plan + Validate (with retry) ────────────────────────
        planner = ExecutionPlanner()
        validator = DryRunValidator()

        enriched_spec: dict[str, Any] = {}
        validation_score = 0
        issues: list[str] = []

        for attempt in range(1, _MAX_PLAN_RETRIES + 2):  # 1, 2, 3
            self._log.info(
                "prompt_enricher.stage2_plan",
                attempt=attempt,
                retry_issues=len(issues),
            )

            # On retry: rebuild normalized with issues noted
            # (RequirementNormalizer is stable, only planner retries)
            try:
                plan = planner.plan(normalized)
                enriched_spec = plan.to_enriched_spec()
            except Exception as exc:
                self._log.error("prompt_enricher.plan_failed", attempt=attempt, error=str(exc))
                if attempt > _MAX_PLAN_RETRIES:
                    return EnrichmentResult(
                        success=False, intent_doc=intent_doc, enriched_spec={},
                        validation_score=0, phases_count=0,
                        request_type=router_result.request_type,
                        error=f"Execution planning failed: {exc}",
                    )
                continue

            # ── Stage 3: Validate ─────────────────────────────────────────────
            self._log.info("prompt_enricher.stage3_validate", attempt=attempt)
            try:
                validation = validator.validate(intent_doc, enriched_spec)
                validation_score = validation.score
            except Exception as exc:
                self._log.warning("prompt_enricher.validate_error", error=str(exc))
                validation_score = 70
                break

            if validation.status == "pass":
                self._log.info(
                    "prompt_enricher.validation_passed",
                    confidence=validation.confidence,
                    summary=validation.summary,
                )
                break

            if validation.is_blocked:
                # Structural problem — replanning won't fix it
                self._log.error(
                    "prompt_enricher.validation_blocked",
                    summary=validation.summary,
                    findings=[f.description for f in validation.critical_findings],
                )
                return EnrichmentResult(
                    success=False,
                    intent_doc=intent_doc,
                    enriched_spec=enriched_spec,
                    validation_score=validation.confidence,
                    phases_count=len(enriched_spec.get("phases", [])),
                    request_type=router_result.request_type,
                    validation_status="block",
                    validation_findings=[
                        {"type": f.type, "severity": f.severity, "description": f.description}
                        for f in validation.critical_findings
                    ],
                    error=f"Blocked: {validation.summary}",
                )

            # status == "revise" — retry with specific instructions
            issues = validation.revise_instructions or validation.issues
            self._log.warning(
                "prompt_enricher.validation_needs_revision",
                attempt=attempt,
                confidence=validation.confidence,
                findings_count=len(validation.findings),
                revise_instructions=issues[:5],
                retrying=attempt <= _MAX_PLAN_RETRIES,
            )
            if attempt > _MAX_PLAN_RETRIES:
                self._log.warning("prompt_enricher.max_retries_hit", final_confidence=validation.confidence)
                break

        phases = enriched_spec.get("phases", [])
        self._log.info(
            "prompt_enricher.done",
            phases_count=len(phases),
            validation_score=validation_score,
            request_type=router_result.request_type,
        )

        # Capture final validation state for DB
        final_status = "pass" if validation_score >= 70 else "revise"
        try:
            final_status = validation.status  # type: ignore[possibly-undefined]
            final_findings = [
                {"type": f.type, "severity": f.severity, "description": f.description}
                for f in validation.findings  # type: ignore[possibly-undefined]
            ]
        except (NameError, AttributeError):
            final_findings = []

        return EnrichmentResult(
            success=bool(phases),
            intent_doc=intent_doc,
            enriched_spec=enriched_spec,
            validation_score=validation_score,
            phases_count=len(phases),
            request_type=router_result.request_type,
            validation_status=final_status,
            validation_findings=final_findings,
        )

    async def persist(self, result: EnrichmentResult) -> None:
        """Persist enrichment result to WorkOrder in Postgres."""
        from sqlalchemy import update  # noqa: PLC0415

        from phalanx.db.models import WorkOrder  # noqa: PLC0415
        from phalanx.db.session import get_db    # noqa: PLC0415

        async with get_db() as session:
            await session.execute(
                update(WorkOrder)
                .where(WorkOrder.id == self.work_order_id)
                .values(
                    intent=result.intent_doc,
                    enriched_spec=result.enriched_spec,
                    current_phase=1 if result.success else 0,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

        self._log.info(
            "prompt_enricher.persisted",
            phases_count=result.phases_count,
            score=result.validation_score,
        )


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.prompt_enricher.enrich_work_order",
    bind=True,
    queue="enricher",
    max_retries=1,
    acks_late=True,
    soft_time_limit=300,   # 5 min: 3 GPT-4o calls + possible retry
    time_limit=600,        # 10 min hard kill
)
def enrich_work_order(  # pragma: no cover
    self,
    work_order_id: str,
    project_id: str,
    raw_prompt: str,
    **kwargs,
) -> dict:
    """
    Celery entry point: enrich a WorkOrder with execution plan.
    Called by the Slack gateway after WorkOrder creation.
    """
    enricher = PromptEnricher(work_order_id=work_order_id, project_id=project_id)
    result = enricher.run(raw_prompt)

    if result.success:
        asyncio.run(enricher.persist(result))
        log.info(
            "enricher.celery.done",
            work_order_id=work_order_id[:8],
            phases=result.phases_count,
            score=result.validation_score,
        )
    else:
        log.error(
            "enricher.celery.failed",
            work_order_id=work_order_id[:8],
            error=result.error,
        )

    return {
        "success": result.success,
        "work_order_id": work_order_id,
        "phases_count": result.phases_count,
        "validation_score": result.validation_score,
        "error": result.error,
    }
