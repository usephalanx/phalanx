"""v1.7.2.7 commander → TL replan-priors wiring tests.

The plan validator's `validate_replan_strategy` was tier-1 unit-tested
in test_plan_validator.py, but it only fires in production when
commander injects `prior_failure_fingerprint`, `prior_task_plan`,
`prior_verify_command`, and `prior_replan_reason` into iter-N+1's TL
ci_context.

These tests pin the wiring so the rule actually fires in prod runs
(not just unit tests).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents._plan_validator import (
    PlanValidationError,
    validate_replan_strategy,
)
from phalanx.agents.cifix_commander import CIFixCommanderAgent


def _make_commander() -> CIFixCommanderAgent:
    return CIFixCommanderAgent(
        run_id="run-replan-test",
        work_order_id="wo-test",
        project_id="proj-test",
    )


def _engineer_task_replace(task_id: str, file_path: str) -> dict:
    return {
        "task_id": task_id,
        "agent": "cifix_engineer",
        "depends_on": [],
        "purpose": "edit a file",
        "steps": [
            {"id": 1, "action": "replace", "file": file_path,
             "old": "x = 1", "new": "x = 2"},
            {"id": 2, "action": "commit", "message": "fix"},
            {"id": 3, "action": "push"},
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. _load_prior_tl_output
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadPriorTLOutput:
    def test_returns_latest_completed_techlead_output(self):
        agent = _make_commander()
        prior_output = {
            "task_plan": [_engineer_task_replace("T2", "src/foo.py")],
            "verify_command": "ruff check src/foo.py",
            "replan_reason": "the previous attempt missed an import",
        }

        async def _run():
            session = MagicMock()
            result_mock = MagicMock()
            result_mock.one_or_none.return_value = (prior_output,)
            session.execute = AsyncMock(return_value=result_mock)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.agents.cifix_commander.get_db", return_value=cm):
                return await agent._load_prior_tl_output()

        out = asyncio.run(_run())
        assert out == prior_output

    def test_returns_none_when_no_prior_techlead(self):
        agent = _make_commander()

        async def _run():
            session = MagicMock()
            result_mock = MagicMock()
            result_mock.one_or_none.return_value = None
            session.execute = AsyncMock(return_value=result_mock)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.agents.cifix_commander.get_db", return_value=cm):
                return await agent._load_prior_tl_output()

        out = asyncio.run(_run())
        assert out is None

    def test_returns_none_when_output_not_dict(self):
        agent = _make_commander()

        async def _run():
            session = MagicMock()
            result_mock = MagicMock()
            result_mock.one_or_none.return_value = ("not a dict",)
            session.execute = AsyncMock(return_value=result_mock)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            with patch("phalanx.agents.cifix_commander.get_db", return_value=cm):
                return await agent._load_prior_tl_output()

        assert asyncio.run(_run()) is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. _build_replan_priors — assembles all 4 fields the validator needs
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildReplanPriors:
    def test_iter_2_includes_all_three_from_prior_tl_plus_fingerprint(self):
        """iter-2: TL has run once (no prior_replan_reason yet on the
        prior emit because that field is only set on iter ≥ 3 emits).
        Three of four fields populated."""
        agent = _make_commander()
        verify_output = {
            "verdict": "new_failures",
            "fingerprint": "deadbeef12345678",
            "new_failures": [{"cmd": "ruff", "exit_code": 1}],
        }
        prior_tl = {
            "task_plan": [_engineer_task_replace("T2", "src/foo.py")],
            "verify_command": "ruff check src/foo.py",
            # no replan_reason on iter-1's TL output
        }

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=prior_tl)
            ):
                return await agent._build_replan_priors(verify_output=verify_output)

        priors = asyncio.run(_run())
        assert priors["prior_failure_fingerprint"] == "deadbeef12345678"
        assert priors["prior_verify_command"] == "ruff check src/foo.py"
        assert priors["prior_task_plan"] == prior_tl["task_plan"]
        assert "prior_replan_reason" not in priors  # not present on iter-1's emit

    def test_iter_3_includes_replan_reason(self):
        """iter-3: prior TL was a REPLAN (iter-2's emit), so its output
        carries a replan_reason. Now all 4 fields populated."""
        agent = _make_commander()
        verify_output = {"fingerprint": "ffff1111"}
        prior_tl = {
            "task_plan": [_engineer_task_replace("T2", "src/bar.py")],
            "verify_command": "pytest tests/test_x.py",
            "replan_reason": "iter-1's replace.old wasn't in the file; tried insert instead",
        }

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=prior_tl)
            ):
                return await agent._build_replan_priors(verify_output=verify_output)

        priors = asyncio.run(_run())
        assert "prior_replan_reason" in priors
        assert "iter-1's replace.old" in priors["prior_replan_reason"]

    def test_missing_fingerprint_skipped(self):
        """If verify_output has no fingerprint (legacy/edge case), the
        helper omits the key — doesn't crash, doesn't fabricate."""
        agent = _make_commander()
        verify_output = {"verdict": "new_failures"}  # no fingerprint
        prior_tl = {"task_plan": [_engineer_task_replace("T2", "src/foo.py")]}

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=prior_tl)
            ):
                return await agent._build_replan_priors(verify_output=verify_output)

        priors = asyncio.run(_run())
        assert "prior_failure_fingerprint" not in priors
        assert "prior_task_plan" in priors

    def test_no_prior_tl_returns_only_fingerprint(self):
        """First iter (no completed TL yet) — only fingerprint flows.
        Caller still benefits from the no-progress signal even without
        prior_task_plan."""
        agent = _make_commander()
        verify_output = {"fingerprint": "abc1234"}

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=None)
            ):
                return await agent._build_replan_priors(verify_output=verify_output)

        priors = asyncio.run(_run())
        assert priors == {"prior_failure_fingerprint": "abc1234"}

    def test_empty_verify_output_returns_empty_dict(self):
        """Edge: verify_output is None or empty. No priors injected."""
        agent = _make_commander()

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=None)
            ):
                return await agent._build_replan_priors(verify_output=None)

        priors = asyncio.run(_run())
        assert priors == {}

    def test_whitespace_only_verify_command_dropped(self):
        """Defensive: a `verify_command` that's just whitespace shouldn't
        flow through — TL doesn't need a useless ' ' string."""
        agent = _make_commander()
        prior_tl = {
            "task_plan": [_engineer_task_replace("T2", "src/foo.py")],
            "verify_command": "   ",
        }

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=prior_tl)
            ):
                return await agent._build_replan_priors(verify_output={"fingerprint": "x"})

        priors = asyncio.run(_run())
        assert "prior_verify_command" not in priors


# ─────────────────────────────────────────────────────────────────────────────
# 3. End-to-end: same-strategy rejected, different-strategy accepted
#    via the wired path (build priors → validate_replan_strategy)
# ─────────────────────────────────────────────────────────────────────────────


class TestEndToEndReplanWiring:
    """Simulates the full chain: commander queries prior TL output,
    builds priors, then validate_replan_strategy fires against them.

    These pin the contract that v1.7.2.7's prompt + validator + this
    commit's commander wiring agree on the same shape."""

    def _setup_priors(self, agent, prior_task_plan, fingerprint="fp1"):
        """Helper: mock _load_prior_tl_output to return a TL output
        with the given task_plan."""
        prior_tl = {
            "task_plan": prior_task_plan,
            "verify_command": "ruff check src/foo.py",
        }
        verify_output = {"fingerprint": fingerprint}

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=prior_tl)
            ):
                return await agent._build_replan_priors(verify_output=verify_output)

        return asyncio.run(_run())

    def test_same_strategy_rejected_end_to_end(self):
        """Commander would inject these priors → TL emits the same
        strategy → validate_replan_strategy raises."""
        agent = _make_commander()
        prior_plan = [_engineer_task_replace("T2", "src/foo.py")]
        priors = self._setup_priors(agent, prior_plan)

        # iter-2's TL emits a plan with the SAME (replace, src/foo.py)
        # signature. Different task_id and tweaked old/new but same shape.
        new_plan = [_engineer_task_replace("T4", "src/foo.py")]

        with pytest.raises(PlanValidationError, match="identical strategy signature"):
            validate_replan_strategy(
                current_plan=new_plan,
                prior_plan=priors["prior_task_plan"],
                iteration=2,
                fix_spec_replan_reason="trying again with different old text",
            )

    def test_different_strategy_passes_end_to_end(self):
        """Pivot to insert on the same file — different signature → accept."""
        agent = _make_commander()
        prior_plan = [_engineer_task_replace("T2", "src/foo.py")]
        priors = self._setup_priors(agent, prior_plan)

        new_plan = [
            {
                "task_id": "T4", "agent": "cifix_engineer",
                "depends_on": [], "purpose": "pivot to insert",
                "steps": [
                    {"id": 1, "action": "insert", "file": "src/foo.py",
                     "after_line": 5, "content": "import httpx\n"},
                    {"id": 2, "action": "commit", "message": "alt"},
                    {"id": 3, "action": "push"},
                ],
            },
        ]

        # No raise expected
        validate_replan_strategy(
            current_plan=new_plan,
            prior_plan=priors["prior_task_plan"],
            iteration=2,
            fix_spec_replan_reason="prior replace failed step_precondition; pivoting to insert",
        )

    def test_different_file_passes_end_to_end(self):
        agent = _make_commander()
        prior_plan = [_engineer_task_replace("T2", "src/foo.py")]
        priors = self._setup_priors(agent, prior_plan)

        new_plan = [_engineer_task_replace("T4", "src/bar.py")]

        validate_replan_strategy(
            current_plan=new_plan,
            prior_plan=priors["prior_task_plan"],
            iteration=2,
            fix_spec_replan_reason="prior fix targeted wrong file; tried bar.py",
        )

    def test_rejected_when_replan_reason_missing(self):
        """Even if strategy differs, missing replan_reason fails the
        validator. Covers the second half of the rule (R1)."""
        agent = _make_commander()
        prior_plan = [_engineer_task_replace("T2", "src/foo.py")]
        priors = self._setup_priors(agent, prior_plan)

        new_plan = [_engineer_task_replace("T4", "src/bar.py")]  # different file

        with pytest.raises(PlanValidationError, match="replan_reason"):
            validate_replan_strategy(
                current_plan=new_plan,
                prior_plan=priors["prior_task_plan"],
                iteration=2,
                fix_spec_replan_reason=None,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Iter-N+1 dispatch carries the priors (proxy via inspect json shape)
# ─────────────────────────────────────────────────────────────────────────────


class TestIterationDispatchInjectsPriors:
    """Verifies that when commander appends iter-N+1 tasks, the JSON
    description carries the four prior_* fields. Proxy for "TL would
    receive these in ci_context"."""

    def test_iter_2_tl_description_carries_all_four_priors(self):
        """End-to-end of `_build_replan_priors` + how the iteration loop
        merges them into iter_ci_context, then how the description gets
        JSON-encoded for the TL task row."""
        import json

        agent = _make_commander()
        verify_output = {"fingerprint": "fp1abc234567"}
        prior_tl = {
            "task_plan": [_engineer_task_replace("T2", "src/foo.py")],
            "verify_command": "ruff check src/foo.py",
            "replan_reason": "first attempt didn't address the right line",
        }

        async def _run():
            with patch.object(
                agent, "_load_prior_tl_output", AsyncMock(return_value=prior_tl)
            ):
                priors = await agent._build_replan_priors(verify_output=verify_output)
            base_ctx = {"repo": "x/y", "pr_number": 1, "branch": "fix/foo"}
            iter_ctx = {**base_ctx, **priors}
            return iter_ctx, json.dumps(iter_ctx)

        iter_ctx, encoded = asyncio.run(_run())
        # All four priors flow through
        assert iter_ctx["prior_failure_fingerprint"] == "fp1abc234567"
        assert iter_ctx["prior_task_plan"] == prior_tl["task_plan"]
        assert iter_ctx["prior_verify_command"] == "ruff check src/foo.py"
        assert iter_ctx["prior_replan_reason"] == "first attempt didn't address the right line"
        # JSON-encodable (description column is text)
        assert "prior_failure_fingerprint" in encoded
        assert "prior_task_plan" in encoded
