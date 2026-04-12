"""
Unit tests for the Phalanx front-door pipeline.

Covers every component that previously had 0% or low coverage:
  - ContextPackage       — dataclass properties and to_context_block()
  - ContextResolver      — all 3 context_type branches (new_work / continuation /
                           conflicting_branch), no-prior-WO path, no-prior-run path
  - IntentRouter         — route(), RouterResult properties, to_context_block(),
                           string-primary_intent coercion
  - RequirementNormalizer — normalize(), temperature selection per request_type,
                           NormalizedSpec.to_dict(), delivery_expectations defaults
  - ExecutionPlanner      — plan(), ExecutionPlan.all_tasks, to_enriched_spec() format,
                           role mapping, _build_claude_prompt output
  - PromptEnricher extras — block → immediate failure, context injection (prior work
                           prefix), normalization failure path

All OpenAI / DB calls are mocked — no real network or I/O.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_scalar_result(obj):
    """Return a mock that behaves like SQLAlchemy's scalar result."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    return r


def _make_mock_wo(
    project_id: str,
    title: str = "Build photoshoot app",
    intent: dict | None = None,
    hours_ago: float = 10,
) -> MagicMock:
    wo = MagicMock()
    wo.id = str(uuid.uuid4())
    wo.project_id = project_id
    wo.title = title
    wo.intent = intent or {"normalized_goal": "some goal", "_request_type": "semi_specified_request"}
    wo.updated_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    return wo


def _make_mock_run(status: str = "COMPLETED", active_branch: str | None = None) -> MagicMock:
    run = MagicMock()
    run.id = str(uuid.uuid4())
    run.status = status
    run.active_branch = active_branch
    run.updated_at = datetime.now(UTC)
    return run


def _make_router_result(
    request_type: str = "semi_specified_request",
    execution_readiness: str = "needs_light_defaults",
    explicit_requirements: list[str] | None = None,
    explicit_constraints: list[str] | None = None,
    inferred_assumptions: list[str] | None = None,
    unknowns: list[str] | None = None,
    risk_flags: list[str] | None = None,
):
    # Use is-not-None check so callers can pass [] to mean "empty, not default"
    reqs = explicit_requirements if explicit_requirements is not None else ["iOS app", "photoshoots"]
    constraints = explicit_constraints if explicit_constraints is not None else ["App Store compliant"]
    assumptions = inferred_assumptions if inferred_assumptions is not None else ["SwiftUI"]
    uknowns_ = unknowns if unknowns is not None else ["payment processor"]
    flags = risk_flags if risk_flags is not None else ["none"]

    from phalanx.agents.intent_router import RouterResult
    return RouterResult(
        request_type=request_type,
        primary_intent={"summary": "Build iOS photoshoot app", "category": "mobile_app"},
        secondary_intents=[],
        explicit_requirements=reqs,
        explicit_constraints=constraints,
        inferred_assumptions=assumptions,
        unknowns=uknowns_,
        execution_readiness=execution_readiness,
        risk_flags=flags,
        recommended_next_step="Proceed with MVP defaults",
        raw={
            "request_type": request_type,
            "primary_intent": {"summary": "Build iOS photoshoot app", "category": "mobile_app"},
            "explicit_requirements": reqs,
        },
    )


def _make_normalized_spec(execution_mode: str = "mvp", is_expert: bool = False):
    from phalanx.agents.requirement_normalizer import NormalizedSpec
    return NormalizedSpec(
        normalized_goal="Build an iOS app for photoshoot bookings",
        artifact_type="mobile_app",
        execution_mode=execution_mode,
        target_users=["Photographers", "Clients"],
        core_user_problem="No easy way to book professional photographers",
        success_criteria=["User books shoot end-to-end"],
        mvp_scope={"in_scope": ["booking", "profiles"], "out_of_scope": ["payments", "video"]},
        functional_requirements=["Photographer profiles", "Booking flow", "Calendar"],
        non_functional_requirements=["iOS 17+", "60fps scrolling"],
        technical_constraints=["App Store compliant"],
        design_requirements=["SwiftUI components"],
        content_requirements=[],
        safe_defaults=["SwiftUI", "MVVM"],
        assumptions=["iOS platform"],
        unresolved_unknowns=["payment processor"],
        delivery_expectations={
            "should_create_branch": True,
            "should_run_build": True,
            "should_run_tests": True,
            "should_open_pr": True,
        },
        raw={"normalized_goal": "Build an iOS app for photoshoot bookings"},
    )


def _make_execution_plan(num_phases: int = 2):
    from phalanx.agents.execution_planner import ExecutionPlan, PlanPhase, PlanTask

    phases = []
    for i in range(1, num_phases + 1):
        tasks = [
            PlanTask(
                task_id=f"t{i}",
                title=f"Task {i}",
                description=f"Description for task {i}",
                owner_role="engineer",
                depends_on=[f"t{i-1}"] if i > 1 else [],
                acceptance_criteria=[f"Criterion {i}a", f"Criterion {i}b"],
                artifacts=[f"File{i}.swift", f"Test{i}.swift"],
                risk_level="low" if i == 1 else "medium",
            )
        ]
        phases.append(PlanPhase(
            phase_name=f"Phase {i}",
            goal=f"Deliver phase {i} goal",
            tasks=tasks,
        ))
    return ExecutionPlan(
        plan_summary="Two-phase incremental iOS build",
        execution_strategy="phased_delivery",
        phases=phases,
        repo_actions={"create_branch": True, "branch_name_suggestion": "feature/photoshoot-ios"},
        verification_plan={"build_checks": ["xcodebuild"], "test_checks": ["xctest"], "manual_review_steps": []},
        open_questions=["Which payment processor?"],
        stop_conditions=["If App Store guideline changes"],
        raw={"plan_summary": "Two-phase incremental iOS build"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# ContextPackage
# ─────────────────────────────────────────────────────────────────────────────


class TestContextPackage:
    def test_has_prior_work_false_when_no_work_order(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(context_type="new_work")
        assert pkg.has_prior_work is False

    def test_has_prior_work_true_when_prior_id_set(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(context_type="continuation", prior_work_order_id="abc-123")
        assert pkg.has_prior_work is True

    def test_is_continuation_true_for_continuation_type(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(context_type="continuation", prior_work_order_id="abc")
        assert pkg.is_continuation is True

    def test_is_continuation_false_for_other_types(self):
        from phalanx.agents.context_resolver import ContextPackage

        assert ContextPackage(context_type="new_work").is_continuation is False
        assert ContextPackage(context_type="conflicting_branch").is_continuation is False

    def test_to_context_block_no_prior_work(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(context_type="new_work")
        block = pkg.to_context_block()
        assert "No prior work orders" in block

    def test_to_context_block_with_prior_work_includes_key_fields(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(
            context_type="continuation",
            prior_work_order_id="wo-123",
            prior_work_order_title="Build photoshoot app",
            last_run_status="COMPLETED",
            hours_since_last_run=24.5,
            active_branch="feature/v1",
            prior_intent_doc={
                "artifact_type": "mobile_app",
                "normalized_goal": "iOS booking app",
            },
            summary="Recent continuation context.",
        )
        block = pkg.to_context_block()
        assert "continuation" in block
        assert "Build photoshoot app" in block
        assert "COMPLETED" in block
        assert "24.5h" in block
        assert "feature/v1" in block
        assert "mobile_app" in block
        assert "iOS booking app" in block
        assert "Recent continuation context." in block

    def test_to_context_block_omits_optional_fields_when_absent(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(
            context_type="continuation",
            prior_work_order_id="wo-456",
            prior_work_order_title="Some title",
            last_run_status="FAILED",
        )
        block = pkg.to_context_block()
        # hours_since_last_run is None — should not appear
        assert "h ago" not in block
        # active_branch is None — should not appear
        assert "active_branch" not in block

    def test_to_context_block_risk_flags_none_not_shown(self):
        from phalanx.agents.context_resolver import ContextPackage

        pkg = ContextPackage(
            context_type="continuation",
            prior_work_order_id="wo-1",
            prior_work_order_title="T",
        )
        block = pkg.to_context_block()
        # risk_flags line only appears in RouterResult.to_context_block, not here — just verify no crash
        assert "[PRIOR WORK CONTEXT]" in block


# ─────────────────────────────────────────────────────────────────────────────
# ContextResolver
# ─────────────────────────────────────────────────────────────────────────────


class TestContextResolver:
    """
    Tests for ContextResolver.resolve().

    Each test builds a mock AsyncSession where session.execute() returns
    pre-configured scalar results for the WorkOrder and Run queries.
    """

    @pytest.mark.asyncio
    async def test_no_prior_workorder_returns_new_work(self):
        from phalanx.agents.context_resolver import ContextResolver

        session = AsyncMock()
        session.execute.return_value = _make_scalar_result(None)

        resolver = ContextResolver(project_id=str(uuid.uuid4()))
        pkg = await resolver.resolve(session)

        assert pkg.context_type == "new_work"
        assert pkg.has_prior_work is False
        assert "First WorkOrder" in pkg.summary

    @pytest.mark.asyncio
    async def test_active_branch_still_running_returns_conflicting_branch(self):
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=5)
        run = _make_mock_run(status="EXECUTING", active_branch="feature/in-progress")

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        assert pkg.context_type == "conflicting_branch"
        assert pkg.active_branch == "feature/in-progress"
        assert "feature/in-progress" in pkg.summary
        assert pkg.has_prior_work is True

    @pytest.mark.asyncio
    async def test_completed_run_with_recent_wo_returns_continuation(self):
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=10)  # within 72h
        run = _make_mock_run(status="COMPLETED", active_branch=None)

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        assert pkg.context_type == "continuation"
        assert pkg.is_continuation is True
        assert pkg.prior_work_order_id == wo.id
        assert pkg.prior_work_order_title == wo.title
        assert pkg.last_run_status == "COMPLETED"
        assert pkg.hours_since_last_run is not None
        assert pkg.hours_since_last_run < 72

    @pytest.mark.asyncio
    async def test_merged_run_with_recent_wo_returns_continuation(self):
        """READY_TO_MERGE status should not block continuation detection."""
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=2)
        run = _make_mock_run(status="READY_TO_MERGE", active_branch="feature/done")

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        # READY_TO_MERGE is in the excluded set — so active_branch shouldn't trigger conflicting
        assert pkg.context_type == "continuation"

    @pytest.mark.asyncio
    async def test_failed_run_with_recent_wo_returns_continuation(self):
        """FAILED status: branch exists but run is done — should be continuation, not conflicting."""
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=5)
        run = _make_mock_run(status="FAILED", active_branch="feature/broken")

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        # FAILED is in the excluded set
        assert pkg.context_type == "continuation"

    @pytest.mark.asyncio
    async def test_old_workorder_beyond_72h_returns_new_work(self):
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=100)  # > 72h
        run = _make_mock_run(status="COMPLETED", active_branch=None)

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        assert pkg.context_type == "new_work"
        assert "not recent" in pkg.summary

    @pytest.mark.asyncio
    async def test_no_prior_run_with_recent_wo_and_intent_returns_continuation(self):
        """WorkOrder exists and is recent but has no runs yet — should be continuation."""
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=1)  # very recent, has intent

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(None),  # no Run yet
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        assert pkg.context_type == "continuation"
        assert pkg.last_run_id is None
        assert pkg.last_run_status is None
        assert pkg.active_branch is None

    @pytest.mark.asyncio
    async def test_prior_intent_doc_extracted_from_workorder(self):
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        intent = {
            "_request_type": "expert_spec",
            "normalized_goal": "Build real-estate platform",
            "artifact_type": "web_app",
        }
        wo = _make_mock_wo(project_id, hours_ago=5, intent=intent)
        run = _make_mock_run(status="COMPLETED")

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        pkg = await resolver.resolve(session)

        assert pkg.prior_intent_doc == intent
        assert pkg.prior_request_type == "expert_spec"

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(self):
        """WorkOrder.updated_at without tzinfo should be treated as UTC."""
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=5)
        # Strip tzinfo to simulate naive datetime from old DB rows
        wo.updated_at = wo.updated_at.replace(tzinfo=None)
        run = _make_mock_run(status="COMPLETED")

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        # Should not raise a TypeError about tz-aware vs tz-naive comparison
        pkg = await resolver.resolve(session)
        assert pkg.hours_since_last_run is not None

    @pytest.mark.asyncio
    async def test_resolve_makes_exactly_two_db_queries(self):
        """ContextResolver must make exactly 2 DB calls — no more."""
        from phalanx.agents.context_resolver import ContextResolver

        project_id = str(uuid.uuid4())
        wo = _make_mock_wo(project_id, hours_ago=10)
        run = _make_mock_run(status="COMPLETED")

        session = AsyncMock()
        session.execute.side_effect = [
            _make_scalar_result(wo),
            _make_scalar_result(run),
        ]

        resolver = ContextResolver(project_id=project_id)
        await resolver.resolve(session)

        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_no_prior_workorder_makes_exactly_one_db_query(self):
        """When there's no prior WO, the Run query should be skipped."""
        from phalanx.agents.context_resolver import ContextResolver

        session = AsyncMock()
        session.execute.return_value = _make_scalar_result(None)

        resolver = ContextResolver(project_id=str(uuid.uuid4()))
        await resolver.resolve(session)

        assert session.execute.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# IntentRouter
# ─────────────────────────────────────────────────────────────────────────────


_SAMPLE_ROUTER_RAW = {
    "request_type": "semi_specified_request",
    "primary_intent": {"summary": "Build iOS app for photoshoots", "category": "mobile_app"},
    "secondary_intents": [],
    "explicit_requirements": ["iOS app", "photoshoot bookings"],
    "explicit_constraints": ["App Store compliant"],
    "inferred_assumptions": ["SwiftUI", "MVVM"],
    "unknowns": ["payment processor"],
    "execution_readiness": "needs_light_defaults",
    "risk_flags": ["none"],
    "recommended_next_step": "Proceed with MVP defaults",
}


class TestIntentRouter:
    def test_route_returns_router_result_with_all_fields(self):
        with patch("phalanx.agents.intent_router.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_ROUTER_RAW

            from phalanx.agents.intent_router import IntentRouter
            result = IntentRouter().route("build an ios app for photoshoot promotions")

        assert result.request_type == "semi_specified_request"
        assert result.primary_intent["category"] == "mobile_app"
        assert "iOS app" in result.explicit_requirements
        assert result.execution_readiness == "needs_light_defaults"
        assert result.risk_flags == ["none"]
        assert result.recommended_next_step == "Proceed with MVP defaults"
        assert result.raw == _SAMPLE_ROUTER_RAW

    def test_route_handles_string_primary_intent(self):
        """If GPT returns primary_intent as a string, coerce it to {summary, category: unknown}."""
        raw = dict(_SAMPLE_ROUTER_RAW)
        raw["primary_intent"] = "Build iOS photoshoot app"  # string instead of dict

        with patch("phalanx.agents.intent_router.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = raw

            from phalanx.agents.intent_router import IntentRouter
            result = IntentRouter().route("build something")

        assert result.primary_intent == {"summary": "Build iOS photoshoot app", "category": "unknown"}

    def test_route_uses_low_temperature(self):
        with patch("phalanx.agents.intent_router.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_ROUTER_RAW

            from phalanx.agents.intent_router import IntentRouter
            IntentRouter().route("anything")

        _, kwargs = mock_client.call.call_args
        assert kwargs.get("temperature", 1.0) <= 0.15

    def test_route_sends_raw_prompt_to_llm(self):
        with patch("phalanx.agents.intent_router.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_ROUTER_RAW

            from phalanx.agents.intent_router import IntentRouter
            IntentRouter().route("my unique prompt payload")

        _, kwargs = mock_client.call.call_args
        messages = kwargs["messages"]
        assert any("my unique prompt payload" in m.get("content", "") for m in messages)

    def test_route_uses_small_token_budget(self):
        with patch("phalanx.agents.intent_router.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_ROUTER_RAW

            from phalanx.agents.intent_router import IntentRouter
            IntentRouter().route("build something")

        _, kwargs = mock_client.call.call_args
        # Router is a classifier — should not use huge token budgets
        assert kwargs.get("max_tokens", 9999) <= 2048

    # RouterResult properties
    def test_can_auto_proceed_true_for_ready_for_normalization(self):
        r = _make_router_result(execution_readiness="ready_for_normalization")
        assert r.can_auto_proceed is True

    def test_can_auto_proceed_true_for_needs_light_defaults(self):
        r = _make_router_result(execution_readiness="needs_light_defaults")
        assert r.can_auto_proceed is True

    def test_can_auto_proceed_false_for_needs_clarification(self):
        r = _make_router_result(execution_readiness="needs_human_clarification")
        assert r.can_auto_proceed is False

    def test_can_auto_proceed_false_for_needs_intent_split(self):
        r = _make_router_result(execution_readiness="needs_intent_split")
        assert r.can_auto_proceed is False

    def test_needs_split_true_for_mixed_multi_intent(self):
        r = _make_router_result(request_type="mixed_multi_intent")
        assert r.needs_split is True

    def test_needs_split_true_for_intent_split_readiness(self):
        r = _make_router_result(execution_readiness="needs_intent_split")
        assert r.needs_split is True

    def test_needs_split_false_for_normal_request(self):
        r = _make_router_result(request_type="semi_specified_request", execution_readiness="needs_light_defaults")
        assert r.needs_split is False

    def test_is_expert_spec_true(self):
        r = _make_router_result(request_type="expert_spec")
        assert r.is_expert_spec is True

    def test_is_expert_spec_false(self):
        r = _make_router_result(request_type="vague_request")
        assert r.is_expert_spec is False

    # to_context_block
    def test_to_context_block_includes_request_type(self):
        r = _make_router_result(request_type="expert_spec")
        block = r.to_context_block()
        assert "expert_spec" in block

    def test_to_context_block_includes_explicit_requirements(self):
        r = _make_router_result(explicit_requirements=["auth", "real-time chat"])
        block = r.to_context_block()
        assert "auth" in block
        assert "real-time chat" in block

    def test_to_context_block_includes_constraints(self):
        r = _make_router_result(explicit_constraints=["HIPAA compliant", "no third-party SDKs"])
        block = r.to_context_block()
        assert "HIPAA compliant" in block
        assert "no third-party SDKs" in block

    def test_to_context_block_includes_unknowns(self):
        r = _make_router_result(unknowns=["preferred payment gateway"])
        block = r.to_context_block()
        assert "preferred payment gateway" in block

    def test_to_context_block_excludes_risk_flags_when_none(self):
        r = _make_router_result(risk_flags=["none"])
        block = r.to_context_block()
        assert "risk_flags" not in block

    def test_to_context_block_includes_non_trivial_risk_flags(self):
        r = _make_router_result(risk_flags=["mixed_intents", "ambiguous_scope"])
        block = r.to_context_block()
        assert "risk_flags" in block
        assert "mixed_intents" in block

    def test_to_context_block_empty_optionals_do_not_appear(self):
        r = _make_router_result(
            explicit_requirements=[],
            explicit_constraints=[],
            inferred_assumptions=[],
            unknowns=[],
            risk_flags=["none"],
        )
        block = r.to_context_block()
        assert "explicit_requirements" not in block
        assert "explicit_constraints" not in block
        assert "unknowns" not in block


# ─────────────────────────────────────────────────────────────────────────────
# RequirementNormalizer
# ─────────────────────────────────────────────────────────────────────────────


_SAMPLE_NORMALIZER_RAW = {
    "normalized_goal": "Build an MVP iOS app for photoshoot bookings",
    "artifact_type": "mobile_app",
    "execution_mode": "mvp",
    "target_users": ["Photographers", "Clients"],
    "core_user_problem": "No easy way to book professional photographers",
    "success_criteria": ["User completes booking in under 2 minutes"],
    "mvp_scope": {
        "in_scope": ["photographer profiles", "booking flow", "calendar"],
        "out_of_scope": ["payments", "video streaming", "web version"],
    },
    "functional_requirements": ["Photographer profiles", "Booking flow", "Calendar view"],
    "non_functional_requirements": ["iOS 17+", "60fps scrolling"],
    "technical_constraints": ["App Store compliant", "Swift only"],
    "design_requirements": ["Match iOS HIG"],
    "content_requirements": [],
    "safe_defaults": ["SwiftUI", "MVVM"],
    "assumptions": ["iOS platform", "Firebase backend"],
    "unresolved_unknowns": ["payment processor choice"],
    "delivery_expectations": {
        "should_create_branch": True,
        "should_run_build": True,
        "should_run_tests": True,
        "should_open_pr": True,
    },
}


class TestRequirementNormalizer:
    def test_normalize_returns_normalized_spec(self):
        router_result = _make_router_result()

        with patch("phalanx.agents.requirement_normalizer.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_NORMALIZER_RAW

            from phalanx.agents.requirement_normalizer import RequirementNormalizer
            result = RequirementNormalizer().normalize(router_result)

        assert result.normalized_goal == "Build an MVP iOS app for photoshoot bookings"
        assert result.artifact_type == "mobile_app"
        assert result.execution_mode == "mvp"
        assert "photographer profiles" in result.mvp_scope["in_scope"]
        assert "payments" in result.mvp_scope["out_of_scope"]
        assert "Photographer profiles" in result.functional_requirements
        assert result.delivery_expectations["should_create_branch"] is True

    def test_expert_spec_uses_temperature_0_1(self):
        router_result = _make_router_result(request_type="expert_spec")
        assert router_result.is_expert_spec is True

        with patch("phalanx.agents.requirement_normalizer.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_NORMALIZER_RAW

            from phalanx.agents.requirement_normalizer import RequirementNormalizer
            RequirementNormalizer().normalize(router_result)

        _, kwargs = mock_client.call.call_args
        assert kwargs.get("temperature") == 0.1

    def test_non_expert_spec_uses_temperature_0_2(self):
        router_result = _make_router_result(request_type="vague_request")
        assert router_result.is_expert_spec is False

        with patch("phalanx.agents.requirement_normalizer.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_NORMALIZER_RAW

            from phalanx.agents.requirement_normalizer import RequirementNormalizer
            RequirementNormalizer().normalize(router_result)

        _, kwargs = mock_client.call.call_args
        assert kwargs.get("temperature") == 0.2

    def test_router_output_is_passed_as_json_payload(self):
        """Normalizer must pass the full router_output JSON, not just the prompt text."""
        import json
        router_result = _make_router_result()

        with patch("phalanx.agents.requirement_normalizer.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_NORMALIZER_RAW

            from phalanx.agents.requirement_normalizer import RequirementNormalizer
            RequirementNormalizer().normalize(router_result)

        _, kwargs = mock_client.call.call_args
        content = kwargs["messages"][0]["content"]
        parsed = json.loads(content)
        assert "router_output" in parsed
        assert parsed["router_output"] == router_result.raw

    def test_to_dict_includes_all_required_fields(self):
        spec = _make_normalized_spec()
        d = spec.to_dict()

        required_keys = {
            "normalized_goal", "artifact_type", "execution_mode", "target_users",
            "core_user_problem", "success_criteria", "mvp_scope",
            "functional_requirements", "non_functional_requirements",
            "technical_constraints", "design_requirements", "content_requirements",
            "safe_defaults", "assumptions", "unresolved_unknowns", "delivery_expectations",
        }
        assert required_keys.issubset(set(d.keys()))

    def test_to_dict_mvp_scope_preserved(self):
        spec = _make_normalized_spec()
        d = spec.to_dict()
        assert "booking" in d["mvp_scope"]["in_scope"]
        assert "payments" in d["mvp_scope"]["out_of_scope"]

    def test_delivery_expectations_defaults_to_true_when_missing(self):
        raw = dict(_SAMPLE_NORMALIZER_RAW)
        del raw["delivery_expectations"]  # simulate GPT omitting this field
        router_result = _make_router_result()

        with patch("phalanx.agents.requirement_normalizer.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = raw

            from phalanx.agents.requirement_normalizer import RequirementNormalizer
            result = RequirementNormalizer().normalize(router_result)

        assert result.delivery_expectations["should_create_branch"] is True
        assert result.delivery_expectations["should_run_build"] is True
        assert result.delivery_expectations["should_run_tests"] is True
        assert result.delivery_expectations["should_open_pr"] is True

    def test_delivery_expectations_false_values_preserved(self):
        raw = dict(_SAMPLE_NORMALIZER_RAW)
        raw["delivery_expectations"] = {
            "should_create_branch": True,
            "should_run_build": False,  # e.g. docs-only task
            "should_run_tests": False,
            "should_open_pr": True,
        }
        router_result = _make_router_result()

        with patch("phalanx.agents.requirement_normalizer.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = raw

            from phalanx.agents.requirement_normalizer import RequirementNormalizer
            result = RequirementNormalizer().normalize(router_result)

        assert result.delivery_expectations["should_run_build"] is False
        assert result.delivery_expectations["should_run_tests"] is False


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionPlanner
# ─────────────────────────────────────────────────────────────────────────────


_SAMPLE_PLANNER_RAW = {
    "plan_summary": "Two-phase incremental iOS MVP: scaffold then booking flow",
    "execution_strategy": "phased_delivery",
    "phases": [
        {
            "phase_name": "Foundation",
            "goal": "Set up the Xcode project with core dependencies",
            "tasks": [
                {
                    "task_id": "t1",
                    "title": "Scaffold Xcode project",
                    "description": "Create new SwiftUI project with MVVM structure",
                    "owner_role": "engineer",
                    "depends_on": [],
                    "acceptance_criteria": ["Project builds without errors", "MVVM folders created"],
                    "artifacts": ["App/", "README.md"],
                    "risk_level": "low",
                }
            ],
        },
        {
            "phase_name": "Booking Flow",
            "goal": "Implement the end-to-end photographer booking experience",
            "tasks": [
                {
                    "task_id": "t2",
                    "title": "Build booking flow",
                    "description": "Implement booking screens per the normalized spec",
                    "owner_role": "engineer",
                    "depends_on": ["t1"],
                    "acceptance_criteria": ["User can complete a booking", "BookingView.swift exists"],
                    "artifacts": ["BookingView.swift", "BookingViewModel.swift"],
                    "risk_level": "medium",
                },
                {
                    "task_id": "t3",
                    "title": "QA booking flow",
                    "description": "Run unit tests for booking",
                    "owner_role": "qa",
                    "depends_on": ["t2"],
                    "acceptance_criteria": ["All tests pass"],
                    "artifacts": ["BookingTests.swift"],
                    "risk_level": "low",
                },
            ],
        },
    ],
    "repo_actions": {
        "create_branch": True,
        "branch_name_suggestion": "feature/photoshoot-ios-mvp",
        "commit_strategy": ["atomic commits per task"],
    },
    "verification_plan": {
        "build_checks": ["xcodebuild clean build"],
        "test_checks": ["xcodebuild test"],
        "manual_review_steps": ["UI review on simulator"],
    },
    "open_questions": ["Which payment provider?"],
    "stop_conditions": ["App Store guideline violation"],
}


class TestExecutionPlanner:
    def test_plan_returns_execution_plan_with_all_fields(self):
        normalized = _make_normalized_spec()

        with patch("phalanx.agents.execution_planner.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_PLANNER_RAW

            from phalanx.agents.execution_planner import ExecutionPlanner
            result = ExecutionPlanner().plan(normalized)

        assert result.plan_summary == "Two-phase incremental iOS MVP: scaffold then booking flow"
        assert result.execution_strategy == "phased_delivery"
        assert len(result.phases) == 2
        assert result.phases[0].phase_name == "Foundation"
        assert result.repo_actions["branch_name_suggestion"] == "feature/photoshoot-ios-mvp"
        assert "xcodebuild clean build" in result.verification_plan["build_checks"]

    def test_plan_parses_tasks_within_phases(self):
        normalized = _make_normalized_spec()

        with patch("phalanx.agents.execution_planner.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_PLANNER_RAW

            from phalanx.agents.execution_planner import ExecutionPlanner
            result = ExecutionPlanner().plan(normalized)

        phase1_tasks = result.phases[0].tasks
        assert len(phase1_tasks) == 1
        assert phase1_tasks[0].task_id == "t1"
        assert phase1_tasks[0].risk_level == "low"
        assert "Project builds without errors" in phase1_tasks[0].acceptance_criteria

        phase2_tasks = result.phases[1].tasks
        assert len(phase2_tasks) == 2
        assert phase2_tasks[0].depends_on == ["t1"]
        assert phase2_tasks[1].owner_role == "qa"

    def test_all_tasks_flat_list_in_order(self):
        plan = _make_execution_plan(num_phases=3)
        tasks = plan.all_tasks
        assert len(tasks) == 3
        assert tasks[0].task_id == "t1"
        assert tasks[1].task_id == "t2"
        assert tasks[2].task_id == "t3"

    def test_planner_uses_normalized_requirements_as_llm_input(self):
        import json
        normalized = _make_normalized_spec()

        with patch("phalanx.agents.execution_planner.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_PLANNER_RAW

            from phalanx.agents.execution_planner import ExecutionPlanner
            ExecutionPlanner().plan(normalized)

        _, kwargs = mock_client.call.call_args
        content = kwargs["messages"][0]["content"]
        parsed = json.loads(content)
        assert "normalized_requirements" in parsed
        # Spot-check a field from our normalized spec
        assert parsed["normalized_requirements"]["artifact_type"] == "mobile_app"

    def test_to_enriched_spec_structure(self):
        """to_enriched_spec must produce Commander-readable format."""
        normalized = _make_normalized_spec()

        with patch("phalanx.agents.execution_planner.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = _SAMPLE_PLANNER_RAW

            from phalanx.agents.execution_planner import ExecutionPlanner
            plan = ExecutionPlanner().plan(normalized)

        spec = plan.to_enriched_spec()

        assert "phases" in spec
        assert "plan_summary" in spec
        assert "execution_strategy" in spec
        assert "repo_actions" in spec
        assert "verification_plan" in spec

        # 3 total tasks (1 + 2) → 3 phase entries
        assert len(spec["phases"]) == 3

    def test_to_enriched_spec_phase_entry_fields(self):
        """Every phase entry must have Commander-required fields."""
        plan = _make_execution_plan(num_phases=1)
        spec = plan.to_enriched_spec()
        entry = spec["phases"][0]

        required = {"id", "name", "agent_role", "role", "context", "objectives",
                    "deliverables", "acceptance_criteria", "rules", "claude_prompt",
                    "_task_id", "_risk_level", "_depends_on"}
        assert required.issubset(set(entry.keys()))

    def test_to_enriched_spec_role_mapping_engineer_to_builder(self):
        plan = _make_execution_plan(num_phases=1)  # owner_role = "engineer"
        spec = plan.to_enriched_spec()
        assert spec["phases"][0]["agent_role"] == "builder"

    def test_to_enriched_spec_role_mapping_qa_stays_qa(self):
        from phalanx.agents.execution_planner import ExecutionPlan, PlanPhase, PlanTask
        task = PlanTask(
            task_id="qa1", title="Run tests", description="Run all QA",
            owner_role="qa", depends_on=[], acceptance_criteria=["All pass"],
            artifacts=["TestSuite.swift"], risk_level="low",
        )
        plan = ExecutionPlan(
            plan_summary="QA phase", execution_strategy="single_pass",
            phases=[PlanPhase(phase_name="QA", goal="Test everything", tasks=[task])],
            repo_actions={}, verification_plan={}, open_questions=[], stop_conditions=[],
        )
        spec = plan.to_enriched_spec()
        assert spec["phases"][0]["agent_role"] == "qa"

    def test_to_enriched_spec_role_mapping_release_stays_release(self):
        from phalanx.agents.execution_planner import ExecutionPlan, PlanPhase, PlanTask
        task = PlanTask(
            task_id="r1", title="Publish", description="Ship it",
            owner_role="release", depends_on=[], acceptance_criteria=["Published"],
            artifacts=["CHANGELOG.md"], risk_level="low",
        )
        plan = ExecutionPlan(
            plan_summary="Release", execution_strategy="single_pass",
            phases=[PlanPhase(phase_name="Release", goal="Ship", tasks=[task])],
            repo_actions={}, verification_plan={}, open_questions=[], stop_conditions=[],
        )
        spec = plan.to_enriched_spec()
        assert spec["phases"][0]["agent_role"] == "release"

    def test_to_enriched_spec_claude_prompt_includes_task_details(self):
        plan = _make_execution_plan(num_phases=1)
        spec = plan.to_enriched_spec()
        prompt = spec["phases"][0]["claude_prompt"]

        # Must include phase context, task description, and criteria
        assert "Phase 1" in prompt
        assert "Task 1" in prompt
        assert "Criterion 1a" in prompt
        assert "File1.swift" in prompt

    def test_to_enriched_spec_phase_ids_are_sequential(self):
        plan = _make_execution_plan(num_phases=3)
        spec = plan.to_enriched_spec()
        ids = [p["id"] for p in spec["phases"]]
        assert ids == [1, 2, 3]

    def test_plan_handles_empty_phases(self):
        raw = dict(_SAMPLE_PLANNER_RAW)
        raw["phases"] = []
        normalized = _make_normalized_spec()

        with patch("phalanx.agents.execution_planner.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = raw

            from phalanx.agents.execution_planner import ExecutionPlanner
            result = ExecutionPlanner().plan(normalized)

        assert result.phases == []
        assert result.all_tasks == []
        spec = result.to_enriched_spec()
        assert spec["phases"] == []


# ─────────────────────────────────────────────────────────────────────────────
# PromptEnricher — additional paths not covered by test_prompt_enricher_unit.py
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptEnricherExtras:
    """
    Covers gaps in the PromptEnricher.run() method:
      - Block status from DryRunValidator → immediate failure (no retry)
      - Context injection: context.has_prior_work → prefix added to prompt
      - Normalization failure path
    """

    def _make_block_validation(self) -> MagicMock:
        v = MagicMock()
        v.status = "block"
        v.passed = False
        v.confidence = 30
        v.score = 30
        v.findings = []
        v.critical_findings = []
        v.is_blocked = True
        v.issues = ["mixed intents cannot be resolved by replanning"]
        v.revise_instructions = []
        v.suggestions = []
        v.summary = "Mixed intents — structural problem."
        return v

    def test_block_validation_returns_failure_immediately(self):
        """When validator returns 'block', run() must return success=False without retrying."""

        router_result = _make_router_result()
        normalized = _make_normalized_spec()
        plan = _make_execution_plan(num_phases=1)
        block_val = self._make_block_validation()

        plan_call_count = []

        def count_plan(self_inner, norm):
            plan_call_count.append(1)
            return plan

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", count_plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=block_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("build something weird")

        assert result.success is False
        assert result.error is not None
        assert "Blocked" in result.error
        # Should NOT retry — planner called exactly once
        assert len(plan_call_count) == 1

    def test_block_validation_sets_validation_status_to_block(self):
        router_result = _make_router_result()
        normalized = _make_normalized_spec()
        plan = _make_execution_plan(num_phases=1)
        block_val = self._make_block_validation()

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", return_value=plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=block_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("mixed request")

        assert result.validation_status == "block"

    def test_normalization_failure_returns_failure(self):
        router_result = _make_router_result()

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch(
                "phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize",
                side_effect=RuntimeError("GPT timeout"),
            ),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("build something")

        assert result.success is False
        assert "Requirement normalization failed" in (result.error or "")
        # intent_doc is {} here — normalization failed before it could be built
        assert result.intent_doc == {}

    def test_context_with_prior_work_injects_prefix_into_router_prompt(self):
        """When context.has_prior_work=True, the context block is prepended to the raw prompt."""
        from phalanx.agents.context_resolver import ContextPackage

        context = ContextPackage(
            context_type="continuation",
            prior_work_order_id="prior-wo-123",
            prior_work_order_title="Prior build",
            last_run_status="COMPLETED",
            hours_since_last_run=5.0,
            summary="Recent continuation.",
        )
        assert context.has_prior_work is True

        captured_prompts = []

        def capture_route(self_inner, prompt):
            captured_prompts.append(prompt)
            return _make_router_result()

        normalized = _make_normalized_spec()
        plan = _make_execution_plan(num_phases=1)

        pass_val = MagicMock()
        pass_val.status = "pass"
        pass_val.confidence = 90
        pass_val.score = 90
        pass_val.is_blocked = False
        pass_val.findings = []
        pass_val.revise_instructions = []
        pass_val.summary = "ok"

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", capture_route),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", return_value=plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=pass_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            PromptEnricher("wo-id", "proj-id").run(
                "add new booking screen",
                context=context,
            )

        assert len(captured_prompts) == 1
        combined = captured_prompts[0]
        # Context block must be prepended
        assert "[PRIOR WORK CONTEXT]" in combined
        assert "Prior build" in combined
        # Original raw prompt must still be present
        assert "add new booking screen" in combined

    def test_context_without_prior_work_does_not_inject_prefix(self):
        """When context.has_prior_work=False, raw_prompt is passed unchanged."""
        from phalanx.agents.context_resolver import ContextPackage

        context = ContextPackage(context_type="new_work")
        assert context.has_prior_work is False

        captured_prompts = []

        def capture_route(self_inner, prompt):
            captured_prompts.append(prompt)
            return _make_router_result()

        normalized = _make_normalized_spec()
        plan = _make_execution_plan(num_phases=1)
        pass_val = MagicMock()
        pass_val.status = "pass"
        pass_val.confidence = 85
        pass_val.score = 85
        pass_val.is_blocked = False
        pass_val.findings = []
        pass_val.revise_instructions = []
        pass_val.summary = "ok"

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", capture_route),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", return_value=plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=pass_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            PromptEnricher("wo-id", "proj-id").run("fresh new request", context=context)

        assert len(captured_prompts) == 1
        assert "[PRIOR WORK CONTEXT]" not in captured_prompts[0]
        assert captured_prompts[0] == "fresh new request"

    def test_enrichment_result_captures_request_type(self):
        router_result = _make_router_result(request_type="expert_spec")
        normalized = _make_normalized_spec()
        plan = _make_execution_plan(num_phases=1)

        pass_val = MagicMock()
        pass_val.status = "pass"
        pass_val.confidence = 92
        pass_val.score = 92
        pass_val.is_blocked = False
        pass_val.findings = []
        pass_val.revise_instructions = []
        pass_val.summary = "ok"

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", return_value=plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=pass_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("expert spec prompt")

        assert result.request_type == "expert_spec"
        assert result.validation_status == "pass"

    def test_enrichment_result_validation_findings_populated_on_failure(self):
        """validation_findings must be a list of dicts (for DB storage)."""
        from phalanx.agents.dry_run_validator import Finding

        router_result = _make_router_result()
        normalized = _make_normalized_spec()
        plan = _make_execution_plan(num_phases=1)

        revise_val = MagicMock()
        revise_val.status = "revise"
        revise_val.confidence = 55
        revise_val.score = 55
        revise_val.is_blocked = False
        revise_val.findings = [
            Finding(
                type="missing_explicit_requirement",
                severity="major",
                description="Booking flow not covered",
                fixable=True,
                suggested_fix="Add booking task",
            )
        ]
        revise_val.critical_findings = []
        revise_val.revise_instructions = ["Add booking task"]
        revise_val.issues = ["Add booking task"]
        revise_val.summary = "Missing booking requirement"

        plan_call_count = [0]

        def count_plan(self_inner, norm):
            plan_call_count[0] += 1
            return plan

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", count_plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=revise_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("build an app")

        # After max retries, still succeeds with last plan
        assert result.success is True
        # validation_findings captured for DB
        assert isinstance(result.validation_findings, list)
        assert result.validation_findings[0]["type"] == "missing_explicit_requirement"
        assert result.validation_findings[0]["severity"] == "major"
