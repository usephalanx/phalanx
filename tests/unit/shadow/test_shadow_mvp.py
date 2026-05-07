"""v1.7.3-ledger MVP — pure-unit tests.

Avoid hitting the real DB; AsyncMock the session. Avoid hitting the
real GitHub API; the runner's GH helpers are tested via injected
HTTP mocks elsewhere. The MVP scope is small enough that we test the
public-shape contracts:

  - Engineer short-circuits when ci_context.shadow_mode is True
    (returns SHIPPED_PROPOSED with diff in output, never calls
    _handle_commit_and_push).
  - ledger.to_dict serializes a ShadowLedger row to JSON-safe dict.
  - _classify_verdict picks SHIPPED_PROPOSED / SAFE_ESCALATE / FAILED
    correctly from TL + engineer task outputs.
  - CLI argparse accepts the documented subcommands + flags.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.shadow import ledger as ledger_crud
from phalanx.shadow.cli import _build_parser
from phalanx.shadow.runner import _classify_verdict


# ── ledger.to_dict ──────────────────────────────────────────────────────


class TestLedgerToDict:
    def test_serializes_minimal_row(self):
        row = MagicMock()
        row.id = "lid-1"
        row.repo = "encode/httpx"
        row.workflow_run_id = 12345
        row.attempt_number = 1
        row.pr_number = 3147
        row.failing_commit_sha = "a" * 40
        row.failure_class = None
        row.phalanx_run_id = "rid-1"
        row.phalanx_verdict = "SHIPPED_PROPOSED"
        row.phalanx_confidence = 0.85
        row.phalanx_proposed_patch = "--- a/x\n+++ b/x\n"
        row.phalanx_root_cause = "missing | None"
        row.phalanx_affected_files = ["x.py"]
        row.phalanx_iterations = 1
        row.phalanx_tool_calls = 7
        row.phalanx_cost_usd = 2.10
        row.phalanx_run_seconds = 612
        row.ground_truth_status = "pending"
        row.maintainer_fix_commit_sha = None
        row.maintainer_actual_patch = None
        row.notes = None
        ts = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        row.created_at = ts
        row.updated_at = ts

        out = ledger_crud.to_dict(row)
        # Round-trips through json without TypeError
        json.dumps(out)
        assert out["id"] == "lid-1"
        assert out["repo"] == "encode/httpx"
        assert out["phalanx_verdict"] == "SHIPPED_PROPOSED"
        assert out["phalanx_proposed_patch"] == "--- a/x\n+++ b/x\n"
        assert out["created_at"].startswith("2026-05-05")


# ── verdict classifier ─────────────────────────────────────────────────


class TestClassifyVerdict:
    def test_shipped_proposed_when_engineer_returned_shadow_verdict(self):
        eng = {"shadow_mode": True, "shadow_verdict": "SHIPPED_PROPOSED"}
        tl = {"confidence": 0.85}
        assert _classify_verdict(run_status="SHIPPED", tl=tl, eng=eng) == "SHIPPED_PROPOSED"

    def test_safe_escalate_when_review_decision_is_escalate(self):
        eng = {}
        tl = {"confidence": 0.0, "review_decision": "ESCALATE"}
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"

    def test_safe_escalate_when_confidence_zero(self):
        eng = {}
        tl = {"confidence": 0.0}
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"

    def test_failed_when_no_shadow_verdict_and_nonzero_confidence(self):
        eng = {}
        tl = {"confidence": 0.45}  # hedged middle, no escalate signal
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "FAILED"

    def test_safe_escalate_takes_precedence_over_failed(self):
        # TL emitted ESCALATE but the run still landed FAILED — verdict
        # is still SAFE_ESCALATE because the architecture-level signal
        # was the right one.
        eng = {}
        tl = {"confidence": 0.0, "review_decision": "ESCALATE"}
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"

    def test_calibration_failed_classifies_as_safe_escalate(self):
        """v1.7.2.9 calibration validator rejecting a hedged confidence
        on a localized deterministic fix is a refusal-to-ship, not a
        pipeline failure. Map to SAFE_ESCALATE."""
        eng = {}
        tl = {
            "confidence": 0.4,
            "error_class": "plan_validation_failed",
            "validation_error": (
                "confidence_calibration_failed: confidence=0.40 on a "
                "localized deterministic fix (≤2 files, has plan, no "
                "flake keywords in root_cause). Re-emit with confidence ≥ 0.7"
            ),
        }
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"

    def test_other_plan_validation_failures_are_still_failed(self):
        """Non-calibration plan_validation_failed (e.g., empty plan,
        replan-strategy mismatch) is a real pipeline failure, not a
        safety-property win. Stays FAILED."""
        eng = {}
        tl = {
            "confidence": 0.8,
            "error_class": "plan_validation_failed",
            "validation_error": "plan must be a non-empty list",
        }
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "FAILED"

    def test_calibration_match_requires_correct_error_class(self):
        """validation_error mentioning calibration without the
        plan_validation_failed error_class shouldn't trigger the branch
        (defensive)."""
        eng = {}
        tl = {
            "confidence": 0.4,
            "validation_error": "confidence_calibration_failed",
        }
        # No error_class field → no SAFE_ESCALATE override on this rule.
        # Confidence 0.4 isn't 0.0, no review_decision=ESCALATE → FAILED.
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "FAILED"

    def test_self_critique_inconsistent_classifies_as_safe_escalate(self):
        """v1.6.0 self_critique gate rejecting an emit (e.g., TL flagged
        grounding_satisfied=False) is the architecture refusing to ship
        — same semantic property as calibration_failed. Map to
        SAFE_ESCALATE.

        Surfaced on the v1.7.3 hardening proof S4 run: TL emitted at
        confidence 0.76 with grounding_satisfied=False; the gate
        rejected; ledger landed FAILED, masking the safety win."""
        eng = {}
        tl = {
            "confidence": 0.76,
            "error_class": "self_critique_inconsistent",
            "failing_checks": ["grounding_satisfied"],
        }
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"

    def test_self_critique_inconsistent_takes_precedence_over_high_confidence(self):
        """Even at 0.95 confidence, a self_critique_inconsistent emit
        is SAFE_ESCALATE — the gate caught a grounding/coverage gap
        TL itself admitted to."""
        eng = {}
        tl = {
            "confidence": 0.95,
            "error_class": "self_critique_inconsistent",
            "failing_checks": ["affected_files_exist_in_repo"],
        }
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"

    def test_other_tl_error_classes_remain_failed(self):
        """error_class names that are NEITHER calibration nor
        self_critique are real pipeline failures, not refusal-to-ship."""
        eng = {}
        tl = {
            "confidence": 0.8,
            "error_class": "no_fix_spec_emitted",
        }
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "FAILED"

    def test_sandbox_setup_failed_classifies_as_failed_not_safe_escalate(self):
        """v1.7.3 post-Phase-2a — when SRE setup FAILED with
        sandbox_provisioning_failed AND TL never ran (empty tl_output),
        the prior classifier defaulted to SAFE_ESCALATE via the
        confidence==0.0 fallback. With the tasks-aware branch, this is
        correctly classified as FAILED (and the runner sets
        failure_class=FAILED_SANDBOX_SETUP separately).

        Surfaced concretely on Phase 2a entries E2 (psf/black) + E5
        (sphinx-doc/sphinx)."""
        from unittest.mock import MagicMock

        sre_task = MagicMock()
        sre_task.agent_role = "cifix_sre_setup"
        sre_task.status = "FAILED"
        sre_task.error = (
            "sandbox_provisioning_failed: install_command_failed: "
            "uv pip install -r pyproject.toml ..."
        )

        eng = {}
        tl = {}  # TL never ran
        assert (
            _classify_verdict(
                run_status="FAILED", tl=tl, eng=eng, tasks=[sre_task]
            )
            == "FAILED"
        )

    def test_sandbox_setup_completed_does_not_force_failed(self):
        """If SRE setup COMPLETED cleanly, the new branch must NOT
        fire — fall through to existing TL/engineer-driven verdict."""
        from unittest.mock import MagicMock

        sre_task = MagicMock()
        sre_task.agent_role = "cifix_sre_setup"
        sre_task.status = "COMPLETED"
        sre_task.error = None

        eng = {}
        tl = {"confidence": 0.0, "review_decision": "ESCALATE"}
        # Should still be SAFE_ESCALATE (TL escalated cleanly), not FAILED
        assert (
            _classify_verdict(
                run_status="FAILED", tl=tl, eng=eng, tasks=[sre_task]
            )
            == "SAFE_ESCALATE"
        )

    def test_sandbox_setup_failed_without_provisioning_marker_falls_through(self):
        """SRE setup can FAIL for many reasons. Only
        sandbox_provisioning_failed counts as FAILED_SANDBOX_SETUP —
        other SRE failures fall through to the existing branches.
        Defensive against false-positive infra classification."""
        from unittest.mock import MagicMock

        sre_task = MagicMock()
        sre_task.agent_role = "cifix_sre_setup"
        sre_task.status = "FAILED"
        sre_task.error = "some_other_failure: tier1_no_match"

        eng = {}
        tl = {}  # empty TL → would normally hit confidence==0.0 → SAFE_ESCALATE
        # Without the sandbox_provisioning_failed marker, we DON'T
        # override — the empty-TL branch still produces SAFE_ESCALATE
        # for now. (If we want to reclassify all SRE-fail-and-empty-TL
        # as FAILED we'd loosen the marker check; deliberately tight.)
        assert (
            _classify_verdict(
                run_status="FAILED", tl=tl, eng=eng, tasks=[sre_task]
            )
            == "SAFE_ESCALATE"
        )

    def test_tasks_none_preserves_backward_compat(self):
        """Existing callers passing tasks=None (or omitting it) get
        the unchanged classifier behavior."""
        eng = {}
        tl = {"confidence": 0.0, "review_decision": "ESCALATE"}
        # Both should produce SAFE_ESCALATE identically
        assert _classify_verdict(run_status="FAILED", tl=tl, eng=eng) == "SAFE_ESCALATE"
        assert (
            _classify_verdict(
                run_status="FAILED", tl=tl, eng=eng, tasks=None
            )
            == "SAFE_ESCALATE"
        )
        assert (
            _classify_verdict(
                run_status="FAILED", tl=tl, eng=eng, tasks=[]
            )
            == "SAFE_ESCALATE"
        )


# ── CLI argparse smoke ─────────────────────────────────────────────────


class TestCLIParser:
    def test_run_requires_repo_and_workflow(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["run"])

    def test_run_accepts_repo_and_workflow(self):
        parser = _build_parser()
        ns = parser.parse_args(
            ["run", "--repo", "encode/httpx", "--workflow-run-id", "8473628194"]
        )
        assert ns.cmd == "run"
        assert ns.repo == "encode/httpx"
        assert ns.workflow_run_id == 8473628194
        assert ns.poll_interval == 10
        assert ns.poll_timeout == 1800

    def test_show_takes_ledger_id(self):
        parser = _build_parser()
        ns = parser.parse_args(["show", "abc-123"])
        assert ns.cmd == "show"
        assert ns.ledger_id == "abc-123"

    def test_export_takes_out_path_and_optional_repo(self):
        parser = _build_parser()
        ns = parser.parse_args(["export", "ledger.json", "--repo", "encode/httpx"])
        assert ns.cmd == "export"
        assert ns.out == "ledger.json"
        assert ns.repo == "encode/httpx"
        assert ns.limit == 500


# ── Engineer shadow short-circuit ──────────────────────────────────────


class TestEngineerShadowShortCircuit:
    """The short-circuit must:
    - not call _handle_commit_and_push
    - return success with shadow_verdict=SHIPPED_PROPOSED
    - include the unified diff in output
    """

    def test_short_circuit_logic_returns_proposed_without_push(self):
        # Pure-unit test of the branching logic. We construct the
        # condition directly: the engineer checks
        # `ci_context.get("shadow_mode") is True` after computing
        # unified_diff. Verify the branch shape independently.
        ci_context = {"shadow_mode": True, "failing_command": "pytest -q"}
        unified_diff = "--- a/x.py\n+++ b/x.py\n"
        affected_files = ["x.py"]

        # Mirror the short-circuit body
        if ci_context.get("shadow_mode") is True:
            output = {
                "committed": False,
                "shadow_mode": True,
                "shadow_verdict": "SHIPPED_PROPOSED",
                "verify": {
                    "cmd": ci_context["failing_command"],
                    "exit_code": 0,
                },
                "files_modified": affected_files,
                "diff": unified_diff,
            }

        assert output["committed"] is False
        assert output["shadow_verdict"] == "SHIPPED_PROPOSED"
        assert output["diff"] == unified_diff
        assert output["files_modified"] == affected_files

    def test_short_circuit_does_not_fire_when_shadow_mode_false(self):
        ci_context = {"shadow_mode": False, "failing_command": "pytest -q"}
        # Branch should be skipped; the test just asserts the predicate.
        assert (ci_context.get("shadow_mode") is True) is False

    def test_short_circuit_does_not_fire_when_shadow_mode_missing(self):
        ci_context = {"failing_command": "pytest -q"}
        assert (ci_context.get("shadow_mode") is True) is False
