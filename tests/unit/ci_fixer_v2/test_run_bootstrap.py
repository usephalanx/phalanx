"""Tests for run_bootstrap — the integration point tying DB, workspace,
sandbox, providers, and the agent loop together.

All external dependencies (DB, v1 workspace/sandbox helpers, providers)
are monkeypatched as module-level seams so the bootstrap's orchestration
is testable without docker, git, or network.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import run_bootstrap as bootstrap
from phalanx.ci_fixer_v2.agent import RunOutcome
from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict
from phalanx.ci_fixer_v2.context import AgentContext, CostRecord


def _make_inputs(**overrides) -> bootstrap.BootstrapInputs:
    defaults = dict(
        ci_fix_run_id="run-1",
        repo_full_name="acme/widget",
        ci_provider="github_actions",
        fingerprint_hash="fp0123456789abcd",
        pr_number=42,
        branch="feature/lint",
        original_failing_command="ruff check app/",
        github_token="ghp_test",
        openai_api_key="sk-openai",
        anthropic_api_key="sk-ant",
        openai_model="gpt-5.4",
        anthropic_model="claude-sonnet-4-6",
        has_write_permission=True,
    )
    defaults.update(overrides)
    return bootstrap.BootstrapInputs(**defaults)


def _patch_seams(
    monkeypatch,
    *,
    inputs: bootstrap.BootstrapInputs | None = None,
    workspace_path: str = "/tmp/ws-test",
    sandbox_container_id: str | None = "container-abc",
    main_llm_returns: RunOutcome,
    main_llm_cost_input: int = 0,
    main_llm_cost_output: int = 0,
    sonnet_cost_input: int = 0,
    sonnet_cost_output: int = 0,
    persist_captured: dict | None = None,
):
    """Install fakes for every external seam. Returns the captured
    persistence payload so tests can assert it."""
    persisted = persist_captured if persist_captured is not None else {}
    inputs_obj = inputs or _make_inputs()

    async def fake_load(_id):
        return inputs_obj

    async def fake_clone(ci_fix_run_id, repo_full_name, branch, github_token):
        assert ci_fix_run_id == inputs_obj.ci_fix_run_id
        return workspace_path

    async def fake_sandbox(_ws):
        return sandbox_container_id

    def fake_main_llm(inputs):
        # Return a callable that, when called, records token usage into
        # the agent's cost record via the passed context (we inject).
        async def _call(_msgs):
            # Simulate a completed run — the outcome is supplied by the test.
            # Token accounting happens here because the real loop normally
            # reads from LLMResponse; the mocked RunOutcome bypasses it.
            return type(
                "R",
                (),
                {
                    "stop_reason": "end_turn",
                    "text": "",
                    "tool_uses": [],
                    "input_tokens": main_llm_cost_input,
                    "output_tokens": main_llm_cost_output,
                    "thinking_tokens": 0,
                },
            )()

        return _call

    def fake_sonnet_llm(_inputs):
        async def _call(_msgs):
            return type(
                "R",
                (),
                {
                    "stop_reason": "end_turn",
                    "text": "",
                    "tool_uses": [],
                    "input_tokens": sonnet_cost_input,
                    "output_tokens": sonnet_cost_output,
                    "thinking_tokens": 0,
                },
            )()

        return _call

    # Bypass the agent loop itself — we test the loop elsewhere. Here we
    # need the OUTCOME to be exactly what the test scripted.
    async def fake_run_loop(ctx: AgentContext, llm_call, max_turns=25):
        # Pretend the loop consumed some tokens so the cost record has
        # meaningful values to assert against.
        ctx.cost.gpt_reasoning_input_tokens += main_llm_cost_input
        ctx.cost.gpt_reasoning_output_tokens += main_llm_cost_output
        ctx.cost.sonnet_coder_input_tokens += sonnet_cost_input
        ctx.cost.sonnet_coder_output_tokens += sonnet_cost_output
        return main_llm_returns

    async def fake_persist(ci_fix_run_id, ctx, outcome):
        persisted["ci_fix_run_id"] = ci_fix_run_id
        persisted["ctx"] = ctx
        persisted["outcome"] = outcome
        persisted["cost"] = ctx.cost

    monkeypatch.setattr(bootstrap, "_load_run_inputs", fake_load)
    monkeypatch.setattr(bootstrap, "_clone_workspace", fake_clone)
    monkeypatch.setattr(bootstrap, "_provision_sandbox", fake_sandbox)
    monkeypatch.setattr(bootstrap, "_build_main_llm", fake_main_llm)
    monkeypatch.setattr(bootstrap, "_build_sonnet_llm", fake_sonnet_llm)
    monkeypatch.setattr(bootstrap, "run_ci_fix_v2", fake_run_loop)
    monkeypatch.setattr(bootstrap, "_persist_run_outcome", fake_persist)
    return persisted


async def test_execute_v2_run_committed_populates_context_and_persists(monkeypatch):
    outcome = RunOutcome(
        verdict=RunVerdict.COMMITTED,
        committed_sha="abcdef",
        committed_branch="feature/lint",
        explanation="fixed",
    )
    persisted = _patch_seams(
        monkeypatch,
        main_llm_returns=outcome,
        main_llm_cost_input=1000,
        main_llm_cost_output=200,
        sonnet_cost_input=500,
        sonnet_cost_output=150,
    )

    result = await bootstrap.execute_v2_run("run-1")

    assert result is outcome
    assert persisted["ci_fix_run_id"] == "run-1"
    ctx: AgentContext = persisted["ctx"]
    # Bootstrap populated ctx from inputs.
    assert ctx.ci_fix_run_id == "run-1"
    assert ctx.repo_full_name == "acme/widget"
    assert ctx.repo_workspace_path == "/tmp/ws-test"
    assert ctx.original_failing_command == "ruff check app/"
    assert ctx.ci_api_key == "ghp_test"
    assert ctx.sandbox_container_id == "container-abc"
    assert ctx.ci_provider == "github_actions"
    assert ctx.fingerprint_hash == "fp0123456789abcd"
    assert ctx.pr_number == 42
    assert ctx.has_write_permission is True
    assert ctx.author_head_branch == "feature/lint"
    # Cost record includes both providers' tokens.
    cost: CostRecord = persisted["cost"]
    assert cost.gpt_reasoning_input_tokens == 1000
    assert cost.gpt_reasoning_output_tokens == 200
    assert cost.sonnet_coder_input_tokens == 500
    assert cost.sonnet_coder_output_tokens == 150
    # finalize_cost_record ran — USD fields are populated.
    assert cost.total_cost_usd > 0


async def test_execute_v2_run_escalation_path(monkeypatch):
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.LOW_CONFIDENCE,
        explanation="two plausible fixes",
    )
    persisted = _patch_seams(monkeypatch, main_llm_returns=outcome)

    await bootstrap.execute_v2_run("run-1")

    assert persisted["outcome"].verdict == RunVerdict.ESCALATED
    assert persisted["outcome"].escalation_reason == EscalationReason.LOW_CONFIDENCE


async def test_execute_v2_run_sandbox_unavailable_still_runs(monkeypatch):
    outcome = RunOutcome(
        verdict=RunVerdict.ESCALATED,
        escalation_reason=EscalationReason.INFRA_FAILURE_OUT_OF_SCOPE,
        explanation="no sandbox",
    )
    persisted = _patch_seams(
        monkeypatch,
        main_llm_returns=outcome,
        sandbox_container_id=None,
    )

    await bootstrap.execute_v2_run("run-1")
    ctx: AgentContext = persisted["ctx"]
    assert ctx.sandbox_container_id is None  # run_in_sandbox will refuse


async def test_execute_v2_run_wires_sonnet_seam_to_real_callable(monkeypatch):
    # Verify that the bootstrap replaces coder_subagent._call_sonnet_llm
    # with the callable built from provider inputs (not the NotImplementedError
    # default).
    outcome = RunOutcome(verdict=RunVerdict.COMMITTED)
    _patch_seams(monkeypatch, main_llm_returns=outcome)

    import phalanx.ci_fixer_v2.coder_subagent as sub_mod

    # Save original and assert bootstrap overwrote it.
    original = sub_mod._call_sonnet_llm
    await bootstrap.execute_v2_run("run-1")
    assert sub_mod._call_sonnet_llm is not original


# ── helper unit tests ─────────────────────────────────────────────────────


def test_map_verdict_to_status():
    assert bootstrap._map_verdict_to_status(RunVerdict.COMMITTED) == "COMMITTED"
    assert bootstrap._map_verdict_to_status(RunVerdict.ESCALATED) == "ESCALATED"
    assert bootstrap._map_verdict_to_status(RunVerdict.FAILED) == "FAILED"


def test_infer_strategy_from_branch():
    assert bootstrap._infer_strategy_from_branch("phalanx/ci-fix/run-1") == "fix_branch"
    assert bootstrap._infer_strategy_from_branch("feature/add-auth") == "author_branch"
    assert bootstrap._infer_strategy_from_branch("") == "author_branch"


# ── Small wrappers around provider builders ───────────────────────────────
# These verify the bootstrap is passing the right model + api key from
# inputs into the provider factories, using the builtin-tool registry.


def test_build_main_llm_wires_openai_model_and_key():
    # Pre-populate the registry so main_agent_tool_schemas() resolves.
    from phalanx.ci_fixer_v2 import tools as tools_pkg

    tools_pkg._register_builtin_tools()

    inputs = _make_inputs(
        openai_model="gpt-5.4",
        openai_api_key="sk-abc",
    )
    callable_ = bootstrap._build_main_llm(inputs)
    assert callable_ is not None  # returned a real LLMCallable
    # Not calling it — that would require HTTP. Shape check only.
    assert callable(callable_)


def test_build_sonnet_llm_wires_anthropic_model_and_key():
    from phalanx.ci_fixer_v2 import tools as tools_pkg

    tools_pkg._register_builtin_tools()

    inputs = _make_inputs(
        anthropic_model="claude-sonnet-4-6",
        anthropic_api_key="sk-ant-abc",
    )
    callable_ = bootstrap._build_sonnet_llm(inputs)
    assert callable(callable_)


def test_bootstrap_inputs_dataclass_fields():
    inputs = _make_inputs()
    # Spot-check field wiring so typos in field names fail loudly.
    assert inputs.repo_full_name == "acme/widget"
    assert inputs.fingerprint_hash == "fp0123456789abcd"
    assert inputs.has_write_permission is True
