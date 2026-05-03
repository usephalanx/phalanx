"""Tier-1 unit tests for v1.7 SRE role split.

Validates that:
  - TaskRouter routes both `cifix_sre_setup` and `cifix_sre_verify` to the
    existing `cifix_sre` Celery task + queue (alias pattern; no new module)
  - Plan validator's V17_AGENT_REGISTRY accepts the new role names
    (already covered indirectly elsewhere; we sanity-check here)
  - Commander persists the new role names (covered in test_v17_commander_dag.py
    + test_dag_persist_shape.py; this file pins the routing layer)
"""

from __future__ import annotations

from phalanx.runtime.task_router import _ROLE_TO_QUEUE, _ROLE_TO_TASK


class TestSreSplitRouting:
    def test_setup_role_routed_to_cifix_sre_queue(self):
        assert _ROLE_TO_QUEUE["cifix_sre_setup"] == "cifix_sre"

    def test_verify_role_routed_to_cifix_sre_queue(self):
        assert _ROLE_TO_QUEUE["cifix_sre_verify"] == "cifix_sre"

    def test_setup_role_dispatches_to_existing_celery_task(self):
        # Both roles map to the same task; the agent dispatches setup vs
        # verify internally via ci_context["sre_mode"].
        assert (
            _ROLE_TO_TASK["cifix_sre_setup"]
            == "phalanx.agents.cifix_sre.execute_task"
        )

    def test_verify_role_dispatches_to_existing_celery_task(self):
        assert (
            _ROLE_TO_TASK["cifix_sre_verify"]
            == "phalanx.agents.cifix_sre.execute_task"
        )

    def test_legacy_cifix_sre_role_still_routed_for_v16_compat(self):
        # Don't break v1.6 testbed runs during cutover — old role still works.
        assert _ROLE_TO_QUEUE["cifix_sre"] == "cifix_sre"
        assert (
            _ROLE_TO_TASK["cifix_sre"] == "phalanx.agents.cifix_sre.execute_task"
        )


class TestPlanValidatorAcceptsSplitRoles:
    def test_split_role_names_in_v17_agent_registry(self):
        from phalanx.agents._v17_types import V17_AGENT_REGISTRY
        assert "cifix_sre_setup" in V17_AGENT_REGISTRY
        assert "cifix_sre_verify" in V17_AGENT_REGISTRY
        assert "cifix_engineer" in V17_AGENT_REGISTRY

    def test_legacy_cifix_sre_NOT_in_v17_registry(self):
        """The plan validator forbids legacy `cifix_sre` in TL's task_plan
        emit — TL must use the split names."""
        from phalanx.agents._v17_types import V17_AGENT_REGISTRY
        assert "cifix_sre" not in V17_AGENT_REGISTRY
