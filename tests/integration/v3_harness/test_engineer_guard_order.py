"""Engineer guard ordering — bug #13 regression guard.

When Tech Lead legitimately concludes "no customer-repo code change possible"
(sandbox env mismatch, CI-infra-only failure, PR meta gate, etc.), it sets:
  - confidence = 0.0
  - affected_files = []
  - failing_command = "" (sometimes — depends on whether TL had any handle)
  - root_cause + fix_spec carry the diagnosis

The engineer's guards must check confidence FIRST so this case yields a
clean `low_confidence` skip with diagnostic context, NOT a misleading
"no failing_command available" error.

Source-level test (no LLM, no DB) — inspects the order of return-statement
text in CIFixEngineerAgent.execute. If a future refactor reorders the
guards, this test fails before deploy.

Bug #13 surfaced 2026-04-28 on humanize regression smoke (run 30063a5e):
TL iter-2 correctly diagnosed `uv/uvx missing in sandbox` with conf 0.0,
empty affected_files, no failing_command. Engineer hit the
no-failing_command guard before the conf check and reported the wrong
error. Fixed by reordering: conf check moves before failing_command.
"""

from __future__ import annotations

import inspect

from phalanx.agents.cifix_engineer import CIFixEngineerAgent


def test_low_confidence_guard_runs_before_failing_command_guard():
    """The `confidence < 0.5` early-return must appear in source BEFORE
    the `no failing_command available` early-return. Otherwise legit
    no-code-fix cases (where TL leaves failing_command empty AND sets
    conf=0.0) get the wrong error."""
    src = inspect.getsource(CIFixEngineerAgent.execute)
    conf_idx = src.find("confidence < 0.5")
    fcmd_idx = src.find("no failing_command available")
    assert conf_idx > 0, "low_confidence guard not found"
    assert fcmd_idx > 0, "failing_command guard not found"
    assert conf_idx < fcmd_idx, (
        f"confidence guard at char {conf_idx} must come before "
        f"failing_command guard at char {fcmd_idx}. "
        "Reordering breaks bug #13's escalation path — when TL says "
        "'no code change possible' (conf=0.0, no failing_command), the "
        "engineer must surface low_confidence skip, not confused by "
        "a misleading missing-failing_command error."
    )


def test_low_confidence_response_carries_tl_diagnosis():
    """When low_confidence skip fires, the response output must include
    TL's root_cause + fix_spec so the commander / debugger can see WHY
    the run was skipped (vs guessing from a generic error string)."""
    src = inspect.getsource(CIFixEngineerAgent.execute)
    # Find the low_confidence return block and check it captures the diagnosis
    conf_section_start = src.find("confidence < 0.5")
    assert conf_section_start > 0
    conf_section = src[conf_section_start : conf_section_start + 1500]
    for required_field in (
        "skipped_reason",
        "tech_lead_confidence",
        "tech_lead_open_questions",
        "tech_lead_root_cause",
        "tech_lead_fix_spec",
    ):
        assert required_field in conf_section, (
            f"low_confidence return must include {required_field!r} so "
            "downstream consumers (commander, scorecard, audit log) can "
            "see why the run skipped. Bug #13 retro: without these "
            "fields the engineer's FAILED state is uninterpretable "
            "without inspecting the upstream TL task by hand."
        )
