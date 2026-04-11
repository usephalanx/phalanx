"""
Unit tests for the PromptEnricher pipeline components.

All OpenAI calls are mocked — no real network calls.
Tests focus on:
  - OpenAIClient: retry logic, JSON parsing, error handling
  - IntentExtractor: extracts intent document from raw prompt
  - PhaseGenerator: generates phase specs from intent doc
  - DryRunValidator: validates phase plan, returns ValidationResult
  - PromptEnricher: orchestrates all steps, handles retries
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_INTENT = {
    "product_type": "ios_app",
    "product_name": "SnapBook",
    "tagline": "Book photoshoots instantly",
    "platform": "iOS 17+",
    "target_users": [
        {"persona": "Photographers", "description": "Professionals managing bookings"},
        {"persona": "Clients", "description": "People booking photoshoots"},
    ],
    "core_problem": "No easy way to book professional photographers",
    "success_definition": "Client books a shoot end-to-end in under 2 minutes",
    "key_features": ["Photographer profiles", "Availability calendar", "Booking flow"],
    "implicit_requirements": ["Push notifications", "Auth", "Payment processing"],
    "tech_stack": {
        "language": "Swift",
        "ui_framework": "SwiftUI",
        "architecture": "MVVM-C",
        "backend": "Firebase",
        "auth": "Sign in with Apple",
        "storage": "CloudKit",
        "key_libraries": [],
        "minimum_version": "iOS 17.0",
    },
    "constraints": ["App Store compliant", "No web version"],
    "non_goals": ["Video streaming", "Built-in editing"],
    "complexity": "medium_high",
    "total_phases": 4,
    "phase_overview": [
        "Phase 1: UX Research & IA",
        "Phase 2: Design System",
        "Phase 3: Core Implementation",
        "Phase 4: Polish & Ship",
    ],
}

SAMPLE_PHASES = {
    "phases": [
        {
            "id": 1,
            "name": "UX Research & Information Architecture",
            "agent_role": "builder",
            "role": {
                "title": "Sr UX Researcher and Product Designer",
                "seniority": "Senior",
                "domain": "Consumer marketplace iOS apps",
                "persona": "You have 12 years designing booking apps at Airbnb and Thumbtack.",
            },
            "context": "Phase 1 of 4. Establishing the UX foundation.",
            "objectives": ["Define user personas", "Map booking flow", "Identify key screens"],
            "deliverables": [
                {"file": "personas.md", "description": "User personas"},
                {"file": "flows.md", "description": "User flows"},
            ],
            "acceptance_criteria": ["2 personas defined", "Happy path documented"],
            "rules": {"do": ["Focus on both sides of marketplace"], "dont": ["Write code yet"]},
            "claude_prompt": (
                "[ROLE]\nYou are a Sr UX Researcher...\n\n"
                "[CONTEXT]\nPhase 1 of 4...\n\n"
                "[OBJECTIVES]\n- Define personas\n\n"
                "[DELIVERABLES]\n- personas.md\n\n"
                "[ACCEPTANCE CRITERIA]\n- 2 personas defined\n\n"
                "Implement all deliverables above."
            ),
        },
        {
            "id": 2,
            "name": "Core Implementation",
            "agent_role": "builder",
            "role": {
                "title": "Staff iOS Engineer",
                "seniority": "Staff",
                "domain": "SwiftUI / MVVM-C",
                "persona": "You have 10 years building iOS apps at Apple and Uber.",
            },
            "context": "Phase 2 of 4. Building on Phase 1 UX deliverables.",
            "objectives": ["Build app shell", "Implement navigation"],
            "deliverables": [{"file": "App.swift", "description": "App entry point"}],
            "acceptance_criteria": ["App builds without errors"],
            "rules": {"do": ["Use SwiftUI"], "dont": ["Use UIKit"]},
            "claude_prompt": "[ROLE]\nYou are a Staff iOS Engineer...\n\nImplement all deliverables.",
        },
    ]
}

SAMPLE_VALIDATION_PASS = {
    "status": "pass",
    "confidence": 88,
    "findings": [],
    "revise_instructions": [],
    "task_findings": [],
    "summary": "Plan fully covers the normalized spec with clear acceptance criteria.",
}

SAMPLE_VALIDATION_REVISE = {
    "status": "revise",
    "confidence": 62,
    "findings": [
        {
            "type": "missing_explicit_requirement",
            "severity": "major",
            "description": "Booking flow listed in functional_requirements but no task covers it",
            "fixable": True,
            "suggested_fix": "Add a task for booking flow implementation in Phase 2",
        },
        {
            "type": "acceptance_criteria_too_vague",
            "severity": "minor",
            "description": "Task t1 acceptance criteria says 'app works' — not testable",
            "fixable": True,
            "suggested_fix": "Replace with specific build/test checks",
        },
    ],
    "revise_instructions": [
        "Add task covering booking flow from functional_requirements",
        "Replace vague acceptance criteria in t1 with specific testable conditions",
    ],
    "task_findings": [
        {"task_id": "t1", "issue": "acceptance criteria too vague", "severity": "minor"},
    ],
    "summary": "Plan missing explicit booking flow requirement; acceptance criteria need specificity.",
}

SAMPLE_VALIDATION_BLOCK = {
    "status": "block",
    "confidence": 45,
    "findings": [
        {
            "type": "mixed_intent_unresolved",
            "severity": "critical",
            "description": "Plan contains tasks for both a mobile app and a web dashboard without separation",
            "fixable": False,
            "suggested_fix": "",
        }
    ],
    "revise_instructions": [],
    "task_findings": [],
    "summary": "Mixed intents unresolved — cannot plan both mobile app and web dashboard in one run.",
}


# ─────────────────────────────────────────────────────────────────────────────
# OpenAIClient
# ─────────────────────────────────────────────────────────────────────────────


def _make_mock_openai_response(content: dict) -> MagicMock:
    """Build a mock OpenAI chat completion response."""
    import json

    choice = MagicMock()
    choice.message.content = json.dumps(content)
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 200
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _make_openai_client_with_mock(mock_responses) -> tuple:
    """
    Returns (OpenAIClient instance, mock_create) with OpenAI patched.
    mock_responses: single response or list of responses/exceptions for side_effect.
    """
    # OpenAI is imported lazily inside __init__, so patch at openai module level
    with patch("openai.OpenAI") as mock_cls:
        mock_inner = MagicMock()
        mock_cls.return_value = mock_inner
        if isinstance(mock_responses, list):
            mock_inner.chat.completions.create.side_effect = mock_responses
        else:
            mock_inner.chat.completions.create.return_value = mock_responses

        from phalanx.agents.openai_client import OpenAIClient

        client = OpenAIClient()
        # Keep reference to mock after context manager exits by returning mock_inner
        return client, mock_inner


class TestOpenAIClient:
    def test_call_returns_parsed_dict(self):
        resp = _make_mock_openai_response({"result": "ok"})
        with patch("openai.OpenAI") as mock_cls:
            mock_inner = MagicMock()
            mock_cls.return_value = mock_inner
            mock_inner.chat.completions.create.return_value = resp

            from phalanx.agents.openai_client import OpenAIClient

            client = OpenAIClient()
            result = client.call(messages=[{"role": "user", "content": "hello"}], system="sys")

        assert result == {"result": "ok"}

    def test_call_retries_on_exception(self):
        import json

        choice = MagicMock()
        choice.message.content = json.dumps({"ok": True})
        success_resp = MagicMock()
        success_resp.choices = [choice]
        success_resp.usage = None

        with patch("openai.OpenAI") as mock_cls:
            with patch("phalanx.agents.openai_client.time.sleep"):
                mock_inner = MagicMock()
                mock_cls.return_value = mock_inner
                mock_inner.chat.completions.create.side_effect = [
                    RuntimeError("rate limit"),
                    RuntimeError("timeout"),
                    success_resp,
                ]

                from phalanx.agents.openai_client import OpenAIClient

                client = OpenAIClient()
                result = client.call(messages=[], system="sys")

        assert result == {"ok": True}
        assert mock_inner.chat.completions.create.call_count == 3

    def test_call_raises_after_max_retries(self):
        with patch("openai.OpenAI") as mock_cls:
            with patch("phalanx.agents.openai_client.time.sleep"):
                mock_inner = MagicMock()
                mock_cls.return_value = mock_inner
                mock_inner.chat.completions.create.side_effect = RuntimeError("persistent error")

                from phalanx.agents.openai_client import OpenAIClient

                client = OpenAIClient()
                with pytest.raises(RuntimeError, match="OpenAI call failed after"):
                    client.call(messages=[], system="sys")

    def test_call_raises_on_invalid_json(self):
        choice = MagicMock()
        choice.message.content = "this is not json {"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = None

        with patch("openai.OpenAI") as mock_cls:
            mock_inner = MagicMock()
            mock_cls.return_value = mock_inner
            mock_inner.chat.completions.create.return_value = resp

            from phalanx.agents.openai_client import OpenAIClient

            client = OpenAIClient()
            with pytest.raises(ValueError, match="invalid JSON"):
                client.call(messages=[], system="sys")


# ─────────────────────────────────────────────────────────────────────────────
# IntentExtractor
# ─────────────────────────────────────────────────────────────────────────────


class TestIntentExtractor:
    def test_extract_returns_intent_document(self):
        with patch("phalanx.agents.intent_extractor.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_INTENT

            from phalanx.agents.intent_extractor import IntentExtractor

            extractor = IntentExtractor()
            result = extractor.extract("build an ios app for photoshoot promotions")

        assert result["product_type"] == "ios_app"
        assert result["product_name"] == "SnapBook"
        assert len(result["target_users"]) == 2
        assert result["total_phases"] == 4
        mock_client.call.assert_called_once()

    def test_extract_passes_raw_prompt_to_gpt(self):
        raw_prompt = "build a website for my wife's real estate business"
        with patch("phalanx.agents.intent_extractor.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = {"product_type": "web_app", "total_phases": 3}

            from phalanx.agents.intent_extractor import IntentExtractor

            IntentExtractor().extract(raw_prompt)

        call_kwargs = mock_client.call.call_args
        messages = call_kwargs[1]["messages"] if call_kwargs[1] else call_kwargs[0][0]
        assert raw_prompt in messages[0]["content"]

    def test_extract_uses_low_temperature(self):
        with patch("phalanx.agents.intent_extractor.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_INTENT

            from phalanx.agents.intent_extractor import IntentExtractor

            IntentExtractor().extract("build something")

        _, kwargs = mock_client.call.call_args
        assert kwargs.get("temperature", 1.0) <= 0.3


# ─────────────────────────────────────────────────────────────────────────────
# PhaseGenerator
# ─────────────────────────────────────────────────────────────────────────────


class TestPhaseGenerator:
    def test_generate_returns_phase_spec(self):
        with patch("phalanx.agents.phase_generator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_PHASES

            from phalanx.agents.phase_generator import PhaseGenerator

            result = PhaseGenerator().generate(SAMPLE_INTENT)

        assert "phases" in result
        assert len(result["phases"]) == 2
        assert result["phases"][0]["name"] == "UX Research & Information Architecture"

    def test_generate_includes_intent_in_prompt(self):
        with patch("phalanx.agents.phase_generator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_PHASES

            from phalanx.agents.phase_generator import PhaseGenerator

            PhaseGenerator().generate(SAMPLE_INTENT)

        _, kwargs = mock_client.call.call_args
        messages = kwargs["messages"]
        assert "SnapBook" in messages[0]["content"]

    def test_generate_includes_issues_on_retry(self):
        issues = ["Missing auth flow", "Phase 2 too vague"]
        with patch("phalanx.agents.phase_generator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_PHASES

            from phalanx.agents.phase_generator import PhaseGenerator

            PhaseGenerator().generate(SAMPLE_INTENT, issues=issues)

        _, kwargs = mock_client.call.call_args
        messages = kwargs["messages"]
        assert "Missing auth flow" in messages[0]["content"]
        assert "Phase 2 too vague" in messages[0]["content"]

    def test_generate_uses_large_token_budget(self):
        with patch("phalanx.agents.phase_generator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_PHASES

            from phalanx.agents.phase_generator import PhaseGenerator

            PhaseGenerator().generate(SAMPLE_INTENT)

        _, kwargs = mock_client.call.call_args
        assert kwargs.get("max_tokens", 0) >= 6000


# ─────────────────────────────────────────────────────────────────────────────
# DryRunValidator
# ─────────────────────────────────────────────────────────────────────────────


class TestDryRunValidator:
    def test_validate_pass_status(self):
        with patch("phalanx.agents.dry_run_validator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_VALIDATION_PASS

            from phalanx.agents.dry_run_validator import DryRunValidator

            result = DryRunValidator().validate(SAMPLE_INTENT, SAMPLE_PHASES)

        assert result.status == "pass"
        assert result.passed is True          # backwards-compat property
        assert result.confidence == 88
        assert result.score == 88             # backwards-compat property
        assert result.findings == []
        assert result.is_blocked is False

    def test_validate_revise_returns_structured_findings(self):
        with patch("phalanx.agents.dry_run_validator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_VALIDATION_REVISE

            from phalanx.agents.dry_run_validator import DryRunValidator

            result = DryRunValidator().validate(SAMPLE_INTENT, SAMPLE_PHASES)

        assert result.status == "revise"
        assert result.passed is False
        assert result.confidence == 62
        assert len(result.findings) == 2
        assert result.findings[0].type == "missing_explicit_requirement"
        assert result.findings[0].fixable is True
        assert result.findings[1].type == "acceptance_criteria_too_vague"
        assert len(result.revise_instructions) == 2
        assert result.is_blocked is False
        # issues property returns revise_instructions for retry loop
        assert "booking flow" in result.issues[0]

    def test_validate_block_on_structural_problem(self):
        with patch("phalanx.agents.dry_run_validator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_VALIDATION_BLOCK

            from phalanx.agents.dry_run_validator import DryRunValidator

            result = DryRunValidator().validate(SAMPLE_INTENT, SAMPLE_PHASES)

        assert result.status == "block"
        assert result.is_blocked is True
        assert result.passed is False
        assert result.findings[0].type == "mixed_intent_unresolved"
        assert result.findings[0].fixable is False

    def test_validate_uses_very_low_temperature(self):
        with patch("phalanx.agents.dry_run_validator.OpenAIClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.call.return_value = SAMPLE_VALIDATION_PASS

            from phalanx.agents.dry_run_validator import DryRunValidator

            DryRunValidator().validate(SAMPLE_INTENT, SAMPLE_PHASES)

        _, kwargs = mock_client.call.call_args
        assert kwargs.get("temperature", 1.0) <= 0.15


# ─────────────────────────────────────────────────────────────────────────────
# PromptEnricher
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptEnricher:
    """
    Tests for the 3-stage PromptEnricher pipeline:
      Stage 0: IntentRouter.route()
      Stage 1: RequirementNormalizer.normalize()
      Stage 2: ExecutionPlanner.plan()
      Stage 3: DryRunValidator.validate()

    All stages are imported lazily inside run() so we patch at the source module level.
    """

    def _make_router_result(self):
        from phalanx.agents.intent_router import RouterResult
        return RouterResult(
            request_type="semi_specified_request",
            primary_intent={"summary": "Build photoshoot iOS app", "category": "mobile_app"},
            secondary_intents=[],
            explicit_requirements=["mobile app", "photoshoots"],
            explicit_constraints=[],
            inferred_assumptions=["iOS platform"],
            unknowns=["payment processing"],
            execution_readiness="needs_light_defaults",
            risk_flags=["none"],
            recommended_next_step="Proceed with MVP defaults",
            raw={"request_type": "semi_specified_request"},
        )

    def _make_normalized_spec(self):
        from phalanx.agents.requirement_normalizer import NormalizedSpec
        return NormalizedSpec(
            normalized_goal="Build an iOS app for managing photoshoots",
            artifact_type="mobile_app",
            execution_mode="mvp",
            target_users=["Photographers", "Clients"],
            core_user_problem="No easy way to book professional photographers",
            success_criteria=["User can book a shoot end-to-end"],
            mvp_scope={"in_scope": ["booking", "profiles"], "out_of_scope": ["payments"]},
            functional_requirements=["Photographer profiles", "Booking flow"],
            non_functional_requirements=["iOS 17+"],
            technical_constraints=["App Store compliant"],
            design_requirements=[],
            content_requirements=[],
            safe_defaults=["SwiftUI", "MVVM"],
            assumptions=["iOS platform"],
            unresolved_unknowns=["payment processing"],
            delivery_expectations={
                "should_create_branch": True,
                "should_run_build": True,
                "should_run_tests": True,
                "should_open_pr": True,
            },
            raw={"normalized_goal": "Build an iOS app for managing photoshoots"},
        )

    def _make_execution_plan(self):
        from phalanx.agents.execution_planner import ExecutionPlan, PlanPhase, PlanTask
        tasks = [
            PlanTask(
                task_id="t1",
                title="Scaffold app structure",
                description="Create Xcode project with MVVM structure",
                owner_role="engineer",
                depends_on=[],
                acceptance_criteria=["App builds successfully"],
                artifacts=["App/", "README.md"],
                risk_level="low",
            ),
            PlanTask(
                task_id="t2",
                title="Build booking flow",
                description="Implement the booking screens",
                owner_role="engineer",
                depends_on=["t1"],
                acceptance_criteria=["User can complete booking"],
                artifacts=["BookingView.swift"],
                risk_level="medium",
            ),
        ]
        return ExecutionPlan(
            plan_summary="Two-phase MVP build: scaffold then booking flow",
            execution_strategy="phased_delivery",
            phases=[PlanPhase(phase_name="Foundation", goal="Set up project", tasks=tasks)],
            repo_actions={"create_branch": True, "branch_name_suggestion": "feature/photoshoot-app"},
            verification_plan={"build_checks": ["xcodebuild"], "test_checks": [], "manual_review_steps": []},
            open_questions=[],
            stop_conditions=[],
            raw={"plan_summary": "Two-phase MVP build"},
        )

    def _make_pass_validation(self, score: int = 88) -> MagicMock:
        v = MagicMock()
        v.status = "pass"
        v.passed = True
        v.confidence = score
        v.score = score
        v.findings = []
        v.critical_findings = []
        v.is_blocked = False
        v.issues = []
        v.revise_instructions = []
        v.suggestions = []
        v.summary = "Plan looks good"
        return v

    def _make_fail_validation(self, issues: list[str], score: int = 55) -> MagicMock:
        v = MagicMock()
        v.status = "revise"
        v.passed = False
        v.confidence = score
        v.score = score
        v.findings = []
        v.critical_findings = []
        v.is_blocked = False
        v.issues = issues
        v.revise_instructions = issues
        v.suggestions = []
        v.summary = "Needs revision"
        return v

    def test_run_success_first_attempt(self):
        router_result = self._make_router_result()
        normalized = self._make_normalized_spec()
        plan = self._make_execution_plan()

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", return_value=plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate",
                  return_value=self._make_pass_validation(88)),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("test-wo-id", "test-proj-id").run(
                "build an ios app for photoshoot promotions"
            )

        assert result.success is True
        assert result.phases_count == 2  # 2 tasks → 2 phases in enriched_spec
        assert result.validation_score == 88
        assert result.request_type == "semi_specified_request"
        assert len(result.enriched_spec["phases"]) == 2

    def test_run_retries_on_validation_failure(self):
        router_result = self._make_router_result()
        normalized = self._make_normalized_spec()
        plan = self._make_execution_plan()
        plan_calls = []

        def fake_plan(self_inner, norm):
            plan_calls.append(True)
            return plan

        validate_results = [
            self._make_fail_validation(["Missing auth flow"]),
            self._make_pass_validation(85),
        ]
        validate_iter = iter(validate_results)

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", fake_plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate",
                  side_effect=lambda *a, **kw: next(validate_iter)),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("build something")

        assert result.success is True
        assert len(plan_calls) == 2  # planned twice (first failed validation)

    def test_run_uses_last_spec_after_max_retries(self):
        router_result = self._make_router_result()
        normalized = self._make_normalized_spec()
        plan = self._make_execution_plan()
        fail_val = self._make_fail_validation(["persistent issue"], score=50)

        with (
            patch("phalanx.agents.intent_router.IntentRouter.route", return_value=router_result),
            patch("phalanx.agents.requirement_normalizer.RequirementNormalizer.normalize", return_value=normalized),
            patch("phalanx.agents.execution_planner.ExecutionPlanner.plan", return_value=plan),
            patch("phalanx.agents.dry_run_validator.DryRunValidator.validate", return_value=fail_val),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("build something")

        # Still succeeds with the last generated plan
        assert result.success is True
        assert result.phases_count == 2

    def test_run_returns_failure_on_intent_error(self):
        with patch(
            "phalanx.agents.intent_router.IntentRouter.route",
            side_effect=RuntimeError("OpenAI down"),
        ):
            from phalanx.agents.prompt_enricher import PromptEnricher
            result = PromptEnricher("wo-id", "proj-id").run("build something")

        assert result.success is False
        assert "Intent routing failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_persist_updates_work_order(self):
        from phalanx.agents.prompt_enricher import EnrichmentResult, PromptEnricher

        enrichment = EnrichmentResult(
            success=True,
            intent_doc=SAMPLE_INTENT,
            enriched_spec=SAMPLE_PHASES,
            validation_score=88,
            phases_count=2,
        )

        with patch("phalanx.db.session.get_db") as mock_get_db:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock()
            mock_session.commit = AsyncMock()
            mock_get_db.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_db.return_value.__aexit__ = AsyncMock(return_value=False)

            enricher = PromptEnricher("wo-id", "proj-id")
            await enricher.persist(enrichment)

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Commander integration: enriched_spec path
# ─────────────────────────────────────────────────────────────────────────────


class TestCommanderEnrichedPath:
    def test_build_plan_from_phase_returns_task_with_role_context(self):
        from phalanx.agents.commander import CommanderAgent

        agent = CommanderAgent(
            run_id="run-id",
            work_order_id="wo-id",
            project_id="proj-id",
        )

        phase = SAMPLE_PHASES["phases"][0]
        wo = MagicMock()
        wo.title = "Test WO"

        plan = agent._build_plan_from_phase(phase, wo)

        tasks = plan["tasks"]
        assert len(tasks) == 1
        task = tasks[0]
        assert "[Phase 1]" in task["title"]
        assert "UX Research & Information Architecture" in task["title"]
        assert task["agent_role"] == "builder"
        assert task["_phase_id"] == 1
        assert task["_phase_name"] == "UX Research & Information Architecture"
        assert "Sr UX Researcher and Product Designer" in task["_role_context"]
        assert "You have 12 years" in task["_role_context"]

    def test_build_plan_from_phase_extracts_deliverable_file_paths(self):
        from phalanx.agents.commander import CommanderAgent

        agent = CommanderAgent(run_id="r", work_order_id="w", project_id="p")
        phase = SAMPLE_PHASES["phases"][0]
        wo = MagicMock()

        plan = agent._build_plan_from_phase(phase, wo)

        files = plan["tasks"][0]["files_likely_touched"]
        assert "personas.md" in files
        assert "flows.md" in files

    @pytest.mark.asyncio
    async def test_generate_task_plan_uses_enriched_spec_when_available(self):
        from phalanx.agents.commander import CommanderAgent

        agent = CommanderAgent(run_id="r", work_order_id="w", project_id="p")

        wo = MagicMock()
        wo.enriched_spec = SAMPLE_PHASES
        wo.current_phase = 1

        plan = await agent._generate_task_plan(wo, "memory block")

        # Should use enriched path (no Claude call)
        tasks = plan["tasks"]
        assert len(tasks) == 1
        assert "[Phase 1]" in tasks[0]["title"]

    @pytest.mark.asyncio
    async def test_generate_task_plan_falls_back_to_claude_without_enriched_spec(self):
        from phalanx.agents.commander import CommanderAgent

        agent = CommanderAgent(run_id="r", work_order_id="w", project_id="p")

        wo = MagicMock()
        wo.enriched_spec = None
        wo.current_phase = 0

        with patch.object(agent, "_plan_via_claude", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"tasks": [{"title": "fallback"}]}
            plan = await agent._generate_task_plan(wo, "memory")

        mock_claude.assert_called_once()
        assert plan["tasks"][0]["title"] == "fallback"

    @pytest.mark.asyncio
    async def test_generate_task_plan_falls_back_if_phase_idx_out_of_range(self):
        from phalanx.agents.commander import CommanderAgent

        agent = CommanderAgent(run_id="r", work_order_id="w", project_id="p")

        wo = MagicMock()
        wo.enriched_spec = SAMPLE_PHASES  # 2 phases
        wo.current_phase = 99  # out of range

        with patch.object(agent, "_plan_via_claude", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = {"tasks": []}
            await agent._generate_task_plan(wo, "memory")

        mock_claude.assert_called_once()
