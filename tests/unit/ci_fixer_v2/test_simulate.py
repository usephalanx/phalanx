"""Unit tests for the simulate CLI.

Every external dep (DB lookup, DB insert, execute_v2_run) is seamed via
module-level functions so this is pure argument-parsing + orchestration
testing. Real-infra validation happens by running the CLI against prod
(that's its job), not in unit tests.
"""

from __future__ import annotations

import argparse

import pytest

from phalanx.ci_fixer_v2 import simulate


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        repo="acme/widget",
        pr=42,
        branch="fail/lint",
        sha="abc1234",
        job_id="job-777",
        failing_command="ruff check .",
        failing_job_name="Lint",
        reuse=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


async def test_simulate_creates_run_and_executes_happy_path(monkeypatch, capsys):
    from phalanx.ci_fixer_v2.agent import RunOutcome
    from phalanx.ci_fixer_v2.config import RunVerdict

    captured = {}

    async def fake_lookup(repo):
        captured["repo"] = repo
        return "integ-abc"

    async def fake_create(args_, integration_id):
        captured["integration_id"] = integration_id
        captured["created_for"] = args_.repo
        return "new-run-uuid"

    async def fake_execute_v2_run(run_id):
        captured["run_id"] = run_id
        return RunOutcome(
            verdict=RunVerdict.COMMITTED,
            committed_sha="deadbeef",
            committed_branch="fail/lint",
            explanation="lint fixed",
        )

    async def fake_print(run_id, outcome):
        captured["printed_run_id"] = run_id
        captured["printed_verdict"] = outcome.verdict.value

    monkeypatch.setattr(simulate, "_lookup_integration_id", fake_lookup)
    monkeypatch.setattr(simulate, "_create_ci_fix_run", fake_create)
    monkeypatch.setattr(simulate, "_print_outcome", fake_print)
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", fake_execute_v2_run)

    rc = await simulate.main_async(_args())
    assert rc == 0  # committed → exit 0
    assert captured["run_id"] == "new-run-uuid"
    assert captured["integration_id"] == "integ-abc"
    assert captured["printed_verdict"] == "committed"


async def test_simulate_reuses_existing_run_when_reuse_set(monkeypatch):
    from phalanx.ci_fixer_v2.agent import RunOutcome
    from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict

    async def fake_lookup(_repo):
        return "integ-abc"

    create_called = {"n": 0}

    async def fake_create(_args, _integ):
        create_called["n"] += 1
        return "SHOULD-NOT-BE-USED"

    async def fake_execute_v2_run(run_id):
        assert run_id == "existing-run-id"
        return RunOutcome(
            verdict=RunVerdict.ESCALATED,
            escalation_reason=EscalationReason.LOW_CONFIDENCE,
            explanation="undecided",
        )

    async def fake_print(_run_id, _outcome):
        return None

    monkeypatch.setattr(simulate, "_lookup_integration_id", fake_lookup)
    monkeypatch.setattr(simulate, "_create_ci_fix_run", fake_create)
    monkeypatch.setattr(simulate, "_print_outcome", fake_print)
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", fake_execute_v2_run)

    rc = await simulate.main_async(_args(reuse="existing-run-id"))
    assert rc == 1  # escalated → non-zero exit
    assert create_called["n"] == 0  # reuse path skipped the insert


async def test_simulate_fails_cleanly_when_integration_missing(
    monkeypatch, capsys
):
    async def fake_lookup(repo):
        raise RuntimeError(f"no CIIntegration row for {repo} — insert one first")

    monkeypatch.setattr(simulate, "_lookup_integration_id", fake_lookup)

    rc = await simulate.main_async(_args(repo="unknown/repo"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "no CIIntegration" in err


async def test_simulate_surfaces_agent_exception_with_traceback(
    monkeypatch, capsys
):
    async def fake_lookup(_repo):
        return "integ-abc"

    async def fake_create(_args, _integ):
        return "run-id"

    async def boom(_run_id):
        raise RuntimeError("sandbox gone")

    monkeypatch.setattr(simulate, "_lookup_integration_id", fake_lookup)
    monkeypatch.setattr(simulate, "_create_ci_fix_run", fake_create)
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", boom)

    rc = await simulate.main_async(_args())
    assert rc == 3
    err = capsys.readouterr().err
    assert "sandbox gone" in err
    # Traceback should be printed to stderr for fast debugging.
    assert "Traceback" in err


async def test_simulate_committed_exits_zero_escalated_exits_one(monkeypatch):
    from phalanx.ci_fixer_v2.agent import RunOutcome
    from phalanx.ci_fixer_v2.config import EscalationReason, RunVerdict

    async def fake_lookup(_repo):
        return "integ-abc"

    async def fake_create(_args, _integ):
        return "run-id"

    async def fake_print(_run_id, _outcome):
        return None

    monkeypatch.setattr(simulate, "_lookup_integration_id", fake_lookup)
    monkeypatch.setattr(simulate, "_create_ci_fix_run", fake_create)
    monkeypatch.setattr(simulate, "_print_outcome", fake_print)
    import phalanx.ci_fixer_v2.run_bootstrap as bootstrap_mod

    # committed → 0
    async def committed(_run_id):
        return RunOutcome(verdict=RunVerdict.COMMITTED, committed_sha="s")

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", committed)
    assert await simulate.main_async(_args()) == 0

    # escalated → 1
    async def escalated(_run_id):
        return RunOutcome(
            verdict=RunVerdict.ESCALATED,
            escalation_reason=EscalationReason.TURN_CAP_REACHED,
        )

    monkeypatch.setattr(bootstrap_mod, "execute_v2_run", escalated)
    assert await simulate.main_async(_args()) == 1
