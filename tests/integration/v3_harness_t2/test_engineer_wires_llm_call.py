"""Catches bug #6 (canary) — cifix_engineer forgot to pass llm_call=
to run_coder_subagent, hitting the test-only stub.

Live symptom we hit during canary #6:
  NotImplementedError: Sonnet LLM wiring lands in Week 1.7. Tests must
  patch `coder_subagent._call_sonnet_llm` with a scripted fake.

Static check: even without real Anthropic, we can assert the CALL SITE
inside cifix_engineer.execute() passes a non-None `llm_call` to
run_coder_subagent. Source-level inspection works because the wiring
is local (not behind dependency-injection that hides it).
"""

from __future__ import annotations

import inspect

from phalanx.agents.cifix_engineer import CIFixEngineerAgent


def test_engineer_execute_calls_run_coder_subagent_with_llm_call():
    """Source-level guard: if a future refactor drops `llm_call=` from
    the call to run_coder_subagent, this test fails before deploy.
    """
    src = inspect.getsource(CIFixEngineerAgent.execute)

    assert "run_coder_subagent(" in src, (
        "engineer no longer calls run_coder_subagent — if intentional, "
        "drop or update this test"
    )
    assert "llm_call=sonnet_llm" in src or "llm_call=" in src, (
        "engineer must explicitly pass llm_call= to run_coder_subagent. "
        "Without it, run_coder_subagent's default _call_sonnet_llm is a "
        "test-only stub that raises NotImplementedError. Bug #6 from canary."
    )


def test_engineer_imports_build_sonnet_coder_callable():
    """Sister check: the source imports build_sonnet_coder_callable from
    v2 providers, since that's what produces a real Sonnet callable.
    """
    src = inspect.getsource(CIFixEngineerAgent.execute)
    assert "build_sonnet_coder_callable" in src, (
        "engineer no longer imports build_sonnet_coder_callable — that "
        "function is what wraps Sonnet's API in an LLMCallable the coder "
        "subagent loop accepts. Without it, llm_call= can't be built."
    )


def test_engineer_imports_coder_tool_schemas():
    """The Sonnet callable needs the coder's tool schemas. Forgetting to
    import + pass them would mean Sonnet has no tools to call (it would
    just emit text, never run sandbox or apply patches).
    """
    src = inspect.getsource(CIFixEngineerAgent.execute)
    assert "coder_subagent_tool_schemas" in src, (
        "engineer no longer imports coder_subagent_tool_schemas — without "
        "it Sonnet has no tools and the run_coder_subagent loop fails."
    )


def test_engineer_imports_coder_subagent_system_prompt():
    """And the coder needs its system prompt from v2."""
    src = inspect.getsource(CIFixEngineerAgent.execute)
    assert "CODER_SUBAGENT_SYSTEM_PROMPT" in src, (
        "engineer no longer imports CODER_SUBAGENT_SYSTEM_PROMPT — the "
        "Sonnet callable needs a system prompt; without one the coder "
        "subagent has no role context."
    )


def test_v2_call_sonnet_llm_is_still_a_stub_so_default_path_is_a_trap():
    """Sanity check: if v2 ever wires _call_sonnet_llm to a real
    implementation, this test is no longer load-bearing — relax it.
    Until then, it documents WHY the engineer must inject llm_call.
    """
    from phalanx.ci_fixer_v2.coder_subagent import _call_sonnet_llm

    src = inspect.getsource(_call_sonnet_llm)
    # The stub raises NotImplementedError. If that's no longer true,
    # someone wired the default — adjust this test.
    assert "NotImplementedError" in src, (
        "_call_sonnet_llm is no longer a stub — engineer's explicit "
        "llm_call= injection is no longer load-bearing. Remove or "
        "downgrade test_engineer_execute_calls_run_coder_subagent_with_llm_call."
    )
