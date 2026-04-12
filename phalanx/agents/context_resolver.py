"""
ContextResolver — pre-Stage 0 of the PromptEnricher pipeline.

Pure DB lookup. No LLM. Deterministic.

Answers one question before any GPT call is made:
  "What do we already know about this project and this user's prior work?"

Output: ContextPackage — injected into IntentRouter so it can classify
  continuation_request correctly and preserve prior decisions.

Context types:
  new_work         — no prior WorkOrders or none recently active
  continuation     — prior WorkOrder exists, same domain, recently active
  conflicting_branch — active branch from prior run, may conflict
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# A WorkOrder is "recent" if it was updated within this window
_RECENCY_WINDOW_HOURS = 72


@dataclass
class ContextPackage:
    """
    Everything the pipeline knows about prior work before GPT is called.
    Injected into IntentRouter.route() as additional context.
    """
    context_type: str                         # new_work | continuation | conflicting_branch
    prior_work_order_id: str | None = None
    prior_work_order_title: str | None = None
    prior_intent_doc: dict[str, Any] = field(default_factory=dict)
    prior_request_type: str | None = None     # from prior enrichment
    active_branch: str | None = None
    last_run_status: str | None = None
    last_run_id: str | None = None
    hours_since_last_run: float | None = None
    summary: str = "No prior context"

    @property
    def has_prior_work(self) -> bool:
        return self.prior_work_order_id is not None

    @property
    def is_continuation(self) -> bool:
        return self.context_type == "continuation"

    def to_context_block(self) -> str:
        """Compact block injected into IntentRouter system prompt."""
        if not self.has_prior_work:
            return "[CONTEXT] No prior work orders for this project."

        lines = [
            "[PRIOR WORK CONTEXT]",
            f"context_type: {self.context_type}",
            f"prior_work_order: {self.prior_work_order_title!r}",
            f"last_run_status: {self.last_run_status}",
        ]
        if self.hours_since_last_run is not None:
            lines.append(f"hours_since_last_run: {self.hours_since_last_run:.1f}h")
        if self.active_branch:
            lines.append(f"active_branch: {self.active_branch}")
        if self.prior_intent_doc:
            artifact = self.prior_intent_doc.get("artifact_type", "")
            goal = self.prior_intent_doc.get("normalized_goal", "")
            if artifact or goal:
                lines.append(f"prior_artifact_type: {artifact}")
                lines.append(f"prior_goal: {goal}")
        lines.append(f"summary: {self.summary}")
        return "\n".join(lines)


class ContextResolver:
    """
    Resolves project context from DB before any LLM call.

    Called once per PromptEnricher.run(). Results are passed to IntentRouter
    so the router can detect continuation_request with full context.
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self._log = log.bind(project_id=project_id[:8], component="context_resolver")

    async def resolve(self, session: AsyncSession) -> ContextPackage:
        """
        Query DB for prior WorkOrders and active runs for this project.

        Args:
            session: Active async DB session (caller owns the context).

        Returns:
            ContextPackage describing prior work state.
        """
        from sqlalchemy import select  # noqa: PLC0415

        from phalanx.db.models import Run, WorkOrder  # noqa: PLC0415

        self._log.info("context_resolver.start")

        # Find the most recently updated WorkOrder for this project
        wo_result = await session.execute(
            select(WorkOrder)
            .where(WorkOrder.project_id == self.project_id)
            .order_by(WorkOrder.updated_at.desc())
            .limit(1)
        )
        prior_wo = wo_result.scalar_one_or_none()

        if prior_wo is None:
            self._log.info("context_resolver.no_prior_work")
            return ContextPackage(
                context_type="new_work",
                summary="First WorkOrder for this project.",
            )

        # Find the most recent Run for that WorkOrder
        run_result = await session.execute(
            select(Run)
            .where(Run.work_order_id == prior_wo.id)
            .order_by(Run.updated_at.desc())
            .limit(1)
        )
        prior_run = run_result.scalar_one_or_none()

        # Calculate recency
        now = datetime.now(UTC)
        last_updated = prior_wo.updated_at
        if last_updated and last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=UTC)
        hours_ago = (
            (now - last_updated).total_seconds() / 3600
            if last_updated else None
        )

        is_recent = hours_ago is not None and hours_ago <= _RECENCY_WINDOW_HOURS

        # Determine context type
        active_branch = prior_run.active_branch if prior_run else None
        last_status = prior_run.status if prior_run else None

        prior_wo_intent = getattr(prior_wo, "intent", None)
        if active_branch and last_status not in ("READY_TO_MERGE", "FAILED"):
            context_type = "conflicting_branch"
            summary = (
                f"Prior run on branch '{active_branch}' is still active "
                f"(status={last_status}). New work may conflict."
            )
        elif is_recent and prior_wo_intent:
            context_type = "continuation"
            summary = (
                f"Recent work on '{prior_wo.title}' "
                f"({hours_ago:.0f}h ago, status={last_status}). "
                "New request may be a continuation."
            )
        else:
            context_type = "new_work"
            summary = (
                f"Prior work exists ('{prior_wo.title}') but is not recent "
                f"({hours_ago:.0f}h ago) — treating as new work."
                if hours_ago else "Treating as new work."
            )

        pkg = ContextPackage(
            context_type=context_type,
            prior_work_order_id=prior_wo.id,
            prior_work_order_title=prior_wo.title,
            prior_intent_doc=prior_wo_intent or {},
            prior_request_type=(prior_wo_intent or {}).get("_request_type"),
            active_branch=active_branch,
            last_run_status=last_status,
            last_run_id=prior_run.id if prior_run else None,
            hours_since_last_run=hours_ago,
            summary=summary,
        )

        self._log.info(
            "context_resolver.done",
            context_type=context_type,
            has_prior_work=pkg.has_prior_work,
            active_branch=active_branch,
            hours_ago=f"{hours_ago:.1f}h" if hours_ago else None,
        )

        return pkg
