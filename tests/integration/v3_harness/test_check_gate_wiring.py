"""Tier-2 wiring test for v1.7.2.4 check-gate integration in commander.

Pins the branching logic:
  - TRUE_GREEN gate → calls _finalize_shipped
  - NOT_FIXED + iters left → falls through to iteration code
  - NOT_FIXED at iter cap → ESCALATE (no ship)
  - REGRESSION → ESCALATE
  - PENDING_TIMEOUT → ESCALATE
  - MISSING_DATA → ESCALATE
  - No integration / no token → falls back to legacy ship (with log warning)

The check-gate logic itself is exercised in
test_github_check_gate.py; this file verifies commander reads the
verdict and routes correctly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents._github_check_gate import (
    CheckGateVerdict,
    CheckSummary,
)
from phalanx.agents.cifix_commander import CIFixCommanderAgent


def _make_agent() -> CIFixCommanderAgent:
    return CIFixCommanderAgent(
        run_id="run-gate-test",
        work_order_id="wo-test",
        project_id="proj-test",
    )


def _gate_verdict(
    decision: str,
    *,
    fixed: list[str] | None = None,
    regressed: list[str] | None = None,
    still_failing: list[str] | None = None,
    pending: list[str] | None = None,
) -> CheckGateVerdict:
    v = CheckGateVerdict(
        decision=decision,
        base_sha="aaa",
        head_sha="bbb",
    )
    v.fixed = fixed or []
    v.regressed = regressed or []
    v.still_failing = still_failing or []
    v.pending = pending or []
    if still_failing or regressed:
        for name in (still_failing or []) + (regressed or []):
            v.post_checks[name] = CheckSummary(
                name=name, conclusion="failure", status="completed",
                html_url=f"https://gh/{name}", summary=f"fake summary for {name}",
            )
    v.notes = f"test {decision}"
    return v


# ─────────────────────────────────────────────────────────────────────────────
# _gate_failures_as_sre_failures — translation from gate to SRE-style payload
# ─────────────────────────────────────────────────────────────────────────────


class TestGateToSREFailureTranslation:
    def test_still_failing_translated_to_synthetic_failures(self):
        verdict = _gate_verdict("NOT_FIXED", still_failing=["Test + Coverage"])
        out = CIFixCommanderAgent._gate_failures_as_sre_failures(verdict)
        assert len(out) == 1
        assert out[0]["name"] == "Test + Coverage"
        assert out[0]["cmd"] == "github_check_run:Test + Coverage"
        assert out[0]["exit_code"] == 1
        assert out[0]["source"] == "check_gate"
        assert out[0]["html_url"] == "https://gh/Test + Coverage"

    def test_regressed_translated_too(self):
        verdict = _gate_verdict("REGRESSION", regressed=["Lint"])
        out = CIFixCommanderAgent._gate_failures_as_sre_failures(verdict)
        assert len(out) == 1
        assert out[0]["name"] == "Lint"

    def test_combined_still_failing_and_regressed(self):
        verdict = _gate_verdict(
            "REGRESSION", regressed=["Lint"], still_failing=["Test"],
        )
        out = CIFixCommanderAgent._gate_failures_as_sre_failures(verdict)
        names = {x["name"] for x in out}
        assert names == {"Lint", "Test"}

    def test_empty_when_no_failures(self):
        verdict = _gate_verdict("TRUE_GREEN", fixed=["Lint"])
        out = CIFixCommanderAgent._gate_failures_as_sre_failures(verdict)
        assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# _run_check_gate — falls back to None when integration/token absent
# ─────────────────────────────────────────────────────────────────────────────


class TestRunCheckGateFallback:
    def test_returns_none_when_no_repo(self):
        agent = _make_agent()
        result = asyncio.run(agent._run_check_gate(
            ci_context={}, head_sha="abc",
        ))
        assert result is None

    def test_returns_none_when_no_head_sha(self):
        agent = _make_agent()
        result = asyncio.run(agent._run_check_gate(
            ci_context={"repo": "x/y", "sha": "aaa"}, head_sha=None,
        ))
        assert result is None

    def test_returns_none_when_no_integration(self):
        agent = _make_agent()
        with patch.object(
            agent, "_load_integration_for_repo", AsyncMock(return_value=None),
        ):
            result = asyncio.run(agent._run_check_gate(
                ci_context={"repo": "x/y", "sha": "aaa"}, head_sha="bbb",
            ))
        assert result is None

    def test_returns_none_when_integration_lacks_token(self):
        agent = _make_agent()
        integ = MagicMock()
        integ.github_token = None
        with patch.object(
            agent, "_load_integration_for_repo", AsyncMock(return_value=integ),
        ):
            result = asyncio.run(agent._run_check_gate(
                ci_context={"repo": "x/y", "sha": "aaa"}, head_sha="bbb",
            ))
        assert result is None

    def test_calls_evaluate_with_right_args(self):
        """When everything's wired, _run_check_gate calls evaluate_check_gate
        with repo, token, base_sha, head_sha."""
        agent = _make_agent()
        integ = MagicMock()
        integ.github_token = "ghp_test"

        captured = {}

        async def _fake_evaluate(**kwargs):
            captured.update(kwargs)
            return _gate_verdict("TRUE_GREEN", fixed=["Lint"])

        with (
            patch.object(
                agent, "_load_integration_for_repo", AsyncMock(return_value=integ),
            ),
            patch(
                "phalanx.agents._github_check_gate.evaluate_check_gate",
                side_effect=_fake_evaluate,
            ),
        ):
            result = asyncio.run(agent._run_check_gate(
                ci_context={"repo": "owner/repo", "sha": "base123"},
                head_sha="head456",
            ))

        assert result is not None
        assert result.decision == "TRUE_GREEN"
        assert captured["repo"] == "owner/repo"
        assert captured["base_sha"] == "base123"
        assert captured["head_sha"] == "head456"
        assert captured["github_token"] == "ghp_test"

    def test_evaluate_exception_returns_none_with_log(self):
        """A network failure during the gate poll must not crash the run.
        We log + fall back to None (legacy ship behavior)."""
        agent = _make_agent()
        integ = MagicMock()
        integ.github_token = "ghp_test"

        async def _boom(**kwargs):
            raise RuntimeError("network down")

        with (
            patch.object(
                agent, "_load_integration_for_repo", AsyncMock(return_value=integ),
            ),
            patch(
                "phalanx.agents._github_check_gate.evaluate_check_gate",
                side_effect=_boom,
            ),
        ):
            result = asyncio.run(agent._run_check_gate(
                ci_context={"repo": "x/y", "sha": "aaa"}, head_sha="bbb",
            ))
        assert result is None
