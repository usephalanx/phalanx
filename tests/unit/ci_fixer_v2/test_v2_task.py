"""Unit tests for the CI Fixer v2 Celery task wrapper.

The task runs in a fresh asyncio loop per Celery invocation; we test
that it correctly hands off to `execute_v2_run` and surfaces the
outcome as a plain-dict result.
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2.agent import RunOutcome
from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict


async def test_execute_v2_task_returns_summary_dict_on_committed(monkeypatch):
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod
    from phalanx.agents.ci_fixer_v2_task import _execute_v2_task_async

    captured = {}

    async def fake_run(ci_fix_run_id: str) -> RunOutcome:
        captured["ci_fix_run_id"] = ci_fix_run_id
        return RunOutcome(
            verdict=RunVerdict.COMMITTED,
            committed_sha="deadbeef",
            committed_branch="feature/fix",
            explanation="fixed",
        )

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", fake_run)

    result = await _execute_v2_task_async("run-abc")

    assert result["ci_fix_run_id"] == "run-abc"
    assert result["verdict"] == "committed"
    assert result["committed_sha"] == "deadbeef"
    assert result["committed_branch"] == "feature/fix"
    assert result["escalation_reason"] is None
    assert captured["ci_fix_run_id"] == "run-abc"


async def test_execute_v2_task_returns_escalation_reason_on_escalated(monkeypatch):
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod
    from phalanx.agents.ci_fixer_v2_task import _execute_v2_task_async

    async def fake_run(ci_fix_run_id: str) -> RunOutcome:
        return RunOutcome(
            verdict=RunVerdict.ESCALATED,
            escalation_reason=EscalationReason.LOW_CONFIDENCE,
            explanation="two plausible fixes",
        )

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", fake_run)

    result = await _execute_v2_task_async("run-esc")
    assert result["verdict"] == "escalated"
    assert result["escalation_reason"] == "low_confidence"
    assert result["committed_sha"] is None


async def test_execute_v2_task_reraises_unhandled_exceptions(monkeypatch):
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod
    from phalanx.agents.ci_fixer_v2_task import _execute_v2_task_async

    async def boom(_id):
        raise RuntimeError("DB gone")

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", boom)

    with pytest.raises(RuntimeError, match="DB gone"):
        await _execute_v2_task_async("run-bad")


def test_execute_v2_task_registered_with_correct_name():
    from phalanx.agents import ci_fixer_v2_task as task_mod

    # The Celery task name is what webhook dispatch references and what
    # queue inspection shows. Locking it down so a rename breaks a test,
    # not prod.
    task = task_mod.execute_v2_task
    assert task.name == "phalanx.agents.ci_fixer_v2.execute_v2_task"
