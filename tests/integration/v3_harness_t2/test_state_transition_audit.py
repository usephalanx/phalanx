"""Catches bug #2 (canary) — _audit signature mismatch when overriding
the base method.

Live symptom we hit during canary #2:
  TypeError: CIFixCommanderAgent._audit() missing 1 required positional
  argument: 'event'

Root cause: I'd overridden _audit with a wrapper using parameter name
`event`, while BaseAgent._audit takes `event_type`. _transition_run
calls self._audit(event_type=...), which the override couldn't handle.

This test runs cifix_commander's _transition_run for real against a
real Run row and asserts it doesn't TypeError. If a future refactor
re-introduces a shadowing override with the wrong signature, the
test catches it before prod.

Real Postgres (test isolation via session-rollback). No real LLM.
"""

from __future__ import annotations

import pytest


pytest_plugins = ["pytest_asyncio"]
pytestmark = pytest.mark.asyncio


async def test_commander_transition_run_does_not_crash_on_audit(
    db_session, cifix_work_order
):
    """_transition_run calls self._audit(event_type=...) internally. If
    _audit's signature is shadowed with a different param name (the
    bug we hit), this raises TypeError before the row is updated.
    """
    from phalanx.agents.cifix_commander import CIFixCommanderAgent
    from phalanx.db.models import Run

    # Create the Run row by hand at status='INTAKE'.
    run = Run(
        work_order_id=cifix_work_order.id,
        project_id=cifix_work_order.project_id,
        run_number=1,
        status="INTAKE",
    )
    db_session.add(run)
    await db_session.flush()

    agent = CIFixCommanderAgent(
        run_id=run.id,
        work_order_id=cifix_work_order.id,
        project_id=cifix_work_order.project_id,
    )

    # The actual call that broke during canary #2. Should NOT raise.
    await agent._transition_run("INTAKE", "RESEARCHING")

    # Verify the row really transitioned (catches a silent no-op
    # version of the bug too).
    await db_session.refresh(run)
    assert run.status == "RESEARCHING", run.status


async def test_commander_does_not_shadow_base_audit_with_bad_signature():
    """Static guard: if a future commit re-adds an _audit override on
    CIFixCommanderAgent, the test fails so the author has to either:
      (a) prove their override is signature-compatible with BaseAgent._audit
      (b) explicitly drop this guard

    The bug we hit was a wrapper that took (self, event, **fields)
    while the base takes (self, event_type, ...). Subclasses are NOT
    expected to override _audit at all.
    """
    from phalanx.agents.base import BaseAgent
    from phalanx.agents.cifix_commander import CIFixCommanderAgent

    # _audit must resolve to BaseAgent's, not a subclass override.
    assert CIFixCommanderAgent._audit is BaseAgent._audit, (
        "CIFixCommanderAgent overrode _audit. The previous override caused "
        "TypeError because _transition_run passes event_type=, not event=. "
        "If you need agent-specific audit logging, do it via a different "
        "method (e.g. self._log.info(...)) — leave BaseAgent._audit alone."
    )


async def test_all_v3_agents_inherit_base_audit_unchanged():
    """Same guard for the other v3 agents — none of them should shadow."""
    from phalanx.agents.base import BaseAgent
    from phalanx.agents.cifix_commander import CIFixCommanderAgent
    from phalanx.agents.cifix_engineer import CIFixEngineerAgent
    from phalanx.agents.cifix_sre import CIFixSREAgent
    from phalanx.agents.cifix_techlead import CIFixTechLeadAgent

    for cls in (
        CIFixCommanderAgent,
        CIFixTechLeadAgent,
        CIFixEngineerAgent,
        CIFixSREAgent,
    ):
        assert cls._audit is BaseAgent._audit, (
            f"{cls.__name__} overrode _audit. See "
            "test_commander_does_not_shadow_base_audit_with_bad_signature "
            "for why this is a foot-gun."
        )
