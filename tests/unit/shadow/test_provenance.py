"""P0-5 — provenance: shadow ledger evidence must be auditable.

Covers the five scenarios from the spec:

  1. Single TL task → ledger matches TL, provenance points at it.
  2. Multiple TL tasks → highest sequence_num selected deterministically.
  3. TL escalates but engineer has confidence → ledger does NOT silently
     record engineer confidence as TL.
  4. SAFE_ESCALATE with empty TL diagnosis → root_cause is synthesized
     with an explicit reason; provenance flags the synthesis.
  5. Every terminal ledger write produces a provenance dict.

Plus cross-cutting:
  - Consistency check fires on a deliberately-injected divergence.
  - Tie-break on equal sequence_num falls back to created_at desc.
  - Engineer task is recorded even when not the chosen source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from phalanx.shadow.provenance import (
    PROVENANCE_SCHEMA_VERSION,
    SOURCE_ROLE_TECHLEAD,
    build_provenance,
    check_consistency,
    synthesize_root_cause_for_safe_escalate,
)


# ── Fixture helpers ───────────────────────────────────────────────────────────


def _task(
    *,
    role: str,
    seq: int,
    output: dict | None = None,
    status: str = "COMPLETED",
    created_at: datetime | None = None,
    task_id: str | None = None,
) -> SimpleNamespace:
    """Build a stand-in for a SQLAlchemy Task row. Provenance only needs
    duck-typing — getattr() on a SimpleNamespace works identically."""
    return SimpleNamespace(
        id=task_id or f"task-{role}-{seq}",
        agent_role=role,
        sequence_num=seq,
        output=output or {},
        status=status,
        created_at=created_at or datetime(2026, 5, 11, 20, 15, seq, tzinfo=timezone.utc),
    )


# ── Scenario 1: single TL task ────────────────────────────────────────────────


class TestSingleTLTask:
    def test_provenance_points_at_the_only_tl_task(self):
        tl = _task(role="cifix_techlead", seq=2, output={
            "confidence": 0.9,
            "root_cause": "narrowing missing on Concatenate branch",
            "review_decision": None,
        })
        tasks = [_task(role="cifix_sre_setup", seq=1), tl]
        prov = build_provenance(tasks)

        assert prov["chosen_source_role"] == SOURCE_ROLE_TECHLEAD
        assert prov["tl_task_id"] == tl.id
        assert prov["tl_task_sequence_num"] == 2
        assert prov["tl_task_count"] == 1
        assert prov["tl_task_confidence"] == 0.9
        assert prov["tl_task_root_cause_head"].startswith("narrowing missing")

    def test_consistency_passes_when_ledger_matches(self):
        tl = _task(role="cifix_techlead", seq=1, output={
            "confidence": 0.9,
            "root_cause": "full root cause text",
        })
        prov = build_provenance([tl])
        diverged, reasons = check_consistency(
            ledger_confidence=0.9,
            ledger_root_cause="full root cause text",
            provenance=prov,
            tl_task_root_cause_full="full root cause text",
        )
        assert diverged is False
        assert reasons == []


# ── Scenario 2: multiple TL tasks → highest sequence wins ─────────────────────


class TestMultipleTLTasks:
    def test_highest_sequence_wins(self):
        tl_iter1 = _task(role="cifix_techlead", seq=2, output={
            "confidence": 0.82,
            "root_cause": "iter-1: CHANGES.md needed",
        })
        tl_iter2 = _task(role="cifix_techlead", seq=5, output={
            "confidence": 0.0,
            "root_cause": "",
            "review_decision": "ESCALATE",
        })
        # Order tasks deliberately scrambled in the list to prove
        # selection isn't sensitive to insertion order.
        tasks = [tl_iter2, _task(role="cifix_sre_setup", seq=1), tl_iter1]
        prov = build_provenance(tasks)

        assert prov["tl_task_id"] == tl_iter2.id, (
            "iter-2 has higher seq, must win"
        )
        assert prov["tl_task_sequence_num"] == 5
        assert prov["tl_task_count"] == 2
        assert prov["tl_task_confidence"] == 0.0
        assert prov["tl_task_review_decision"] == "ESCALATE"

    def test_tie_breaks_on_created_at_desc(self):
        """When sequence_num is equal (shouldn't happen in practice but
        the data model allows it), the more recent task wins."""
        t_old = _task(
            role="cifix_techlead",
            seq=2,
            output={"confidence": 0.5, "root_cause": "old"},
            created_at=datetime(2026, 5, 11, 20, 0, 0, tzinfo=timezone.utc),
        )
        t_new = _task(
            role="cifix_techlead",
            seq=2,
            output={"confidence": 0.7, "root_cause": "new"},
            created_at=datetime(2026, 5, 11, 21, 0, 0, tzinfo=timezone.utc),
        )
        prov = build_provenance([t_old, t_new])
        assert prov["tl_task_confidence"] == 0.7

    def test_selection_is_deterministic_across_runs(self):
        """Same inputs, same outputs — no dependence on dict ordering or
        Python set iteration."""
        tasks = [
            _task(role="cifix_techlead", seq=2, output={"confidence": 0.5}),
            _task(role="cifix_techlead", seq=4, output={"confidence": 0.8}),
            _task(role="cifix_techlead", seq=3, output={"confidence": 0.6}),
        ]
        runs = [build_provenance(tasks) for _ in range(5)]
        ids = {r["tl_task_id"] for r in runs}
        assert len(ids) == 1, "selection drifted across identical inputs"


# ── Scenario 3: TL escalates, engineer has different confidence ───────────────


class TestNoSilentFallbackToEngineer:
    def test_tl_escalate_engineer_high_confidence_no_silent_takeover(self):
        """If TL escalates with conf=0.0 and the engineer also produced a
        confidence number, the ledger MUST NOT silently record the
        engineer's confidence as TL's. Provenance makes this auditable."""
        tl = _task(role="cifix_techlead", seq=3, output={
            "confidence": 0.0,
            "review_decision": "ESCALATE",
            "root_cause": "TL refused — grounding gap",
        })
        eng = _task(role="cifix_engineer", seq=4, output={
            "confidence": 0.9,   # engineer's own confidence on a partial draft
        })
        prov = build_provenance([tl, eng])

        # chosen source is TL regardless of engineer's confidence.
        assert prov["chosen_source_role"] == SOURCE_ROLE_TECHLEAD
        assert prov["tl_task_confidence"] == 0.0
        # Engineer task is recorded for cross-reference, NOT promoted.
        assert prov["engineer_task_id"] == eng.id
        assert prov["engineer_task_confidence"] == 0.9
        # Sanity: the two confidences are visibly different in the provenance,
        # so any audit immediately sees the architecture's TL preference.
        assert prov["tl_task_confidence"] != prov["engineer_task_confidence"]


# ── Scenario 4: SAFE_ESCALATE with empty diagnosis ────────────────────────────


class TestEmptyDiagnosisSynthesis:
    def test_synthesize_for_zero_confidence(self):
        txt = synthesize_root_cause_for_safe_escalate(
            "tl_zero_confidence", {"confidence": 0.0}
        )
        assert "Phalanx escalated" in txt
        assert "confidence=0.0" in txt
        assert len(txt) > 30, "synthesized text must be substantive"

    def test_synthesize_for_calibration_failure(self):
        txt = synthesize_root_cause_for_safe_escalate(
            "calibration_failed", {}
        )
        assert "calibration" in txt.lower()

    def test_synthesize_for_self_critique(self):
        txt = synthesize_root_cause_for_safe_escalate(
            "self_critique_inconsistent", {}
        )
        assert "self-critique" in txt.lower() or "grounding" in txt.lower()

    def test_synthesize_for_explicit_escalate(self):
        txt = synthesize_root_cause_for_safe_escalate(
            "tl_escalated", {"review_decision": "ESCALATE"}
        )
        assert "ESCALATE" in txt or "escalat" in txt.lower()


# ── Scenario 5: provenance always present on terminal write ───────────────────


class TestProvenanceAlwaysPresent:
    def test_build_provenance_never_returns_none(self):
        """Even with zero tasks, provenance is structured — not None."""
        prov = build_provenance([])
        assert prov is not None
        assert prov["_schema_version"] == PROVENANCE_SCHEMA_VERSION
        assert prov["chosen_source_role"] == SOURCE_ROLE_TECHLEAD
        assert prov["tl_task_count"] == 0
        assert prov["tl_task_id"] is None
        # divergence flag defaults to False — set explicitly by the writer
        # only when the consistency check fires.
        assert prov["divergence_detected"] is False

    def test_schema_version_pinned(self):
        prov = build_provenance([_task(role="cifix_techlead", seq=1)])
        # P1-6: bumped to 2 (added sre_setup_diagnostic).
        # v3: bumped to 3 (added verification_evidence before/after block).
        # If you're seeing this fail, you're changing the schema —
        # update docs/ops/ledger-auditing.md and ledger-reconciliation.md.
        assert prov["_schema_version"] == 3

    def test_all_documented_keys_present(self):
        """The doc'd schema is the contract. Audit tools depend on it."""
        prov = build_provenance([
            _task(role="cifix_techlead", seq=1, output={"confidence": 0.5}),
            _task(role="cifix_engineer", seq=2, output={"confidence": 0.7}),
        ])
        required = {
            "_schema_version", "chosen_source_role", "chosen_source_reason",
            "tl_task_id", "tl_task_created_at", "tl_task_sequence_num",
            "tl_task_status", "tl_task_confidence", "tl_task_review_decision",
            "tl_task_root_cause_head", "tl_task_count",
            "engineer_task_id", "engineer_task_status", "engineer_task_confidence",
            "root_cause_synthesized", "root_cause_synthesis_reason",
            "divergence_detected", "divergence_details",
            "sre_setup_diagnostic",  # P1-6
        }
        missing = required - set(prov.keys())
        assert not missing, f"provenance missing keys: {missing}"


# ── Cross-cutting: consistency check fires on divergence ──────────────────────


class TestDivergenceDetection:
    def test_diverging_confidence_is_flagged(self):
        """The W1.9 shape: ledger says one thing, source task says another."""
        tl = _task(role="cifix_techlead", seq=1, output={
            "confidence": 0.0,
            "root_cause": "",
            "review_decision": "ESCALATE",
        })
        prov = build_provenance([tl])
        diverged, reasons = check_consistency(
            ledger_confidence=0.82,   # ← injected divergence
            ledger_root_cause="something",
            provenance=prov,
            tl_task_root_cause_full="",
        )
        assert diverged is True
        assert any("confidence" in r.lower() for r in reasons)

    def test_diverging_root_cause_is_flagged_unless_synthesized(self):
        tl = _task(role="cifix_techlead", seq=1, output={
            "confidence": 0.9,
            "root_cause": "real reason from TL",
        })
        prov = build_provenance([tl])
        diverged, reasons = check_consistency(
            ledger_confidence=0.9,
            ledger_root_cause="DIFFERENT TEXT",
            provenance=prov,
            tl_task_root_cause_full="real reason from TL",
        )
        assert diverged is True
        assert any("root_cause" in r for r in reasons)

    def test_synthesized_root_cause_does_not_flag_divergence(self):
        """When the writer synthesizes a root_cause for an empty diagnosis,
        the consistency check must not flag that as divergence."""
        tl = _task(role="cifix_techlead", seq=1, output={
            "confidence": 0.0,
            "root_cause": "",
            "review_decision": "ESCALATE",
        })
        prov = build_provenance(
            [tl],
            root_cause_synthesized=True,
            root_cause_synthesis_reason="tl_escalated",
        )
        diverged, reasons = check_consistency(
            ledger_confidence=0.0,
            ledger_root_cause="Phalanx escalated: TL emitted...",
            provenance=prov,
            tl_task_root_cause_full="",
        )
        assert diverged is False, reasons


# ── Reproduction of W1.9 shape with provenance in place ───────────────────────


class TestW19ForwardFixReproduction:
    """W1.9 historical row is gone (DB wiped on 2026-05-11). Prove the
    forward fix by synthesizing the multi-iteration-divergence shape and
    asserting the provenance is unambiguous about which task the ledger
    came from."""

    def test_two_tl_iterations_diverging_confidence_unambiguously_attributed(self):
        # Iteration 1: produced confidence=0.82 + CHANGES.md root_cause
        tl_1 = _task(
            role="cifix_techlead", seq=2,
            task_id="tl-iter-1",
            output={
                "confidence": 0.82,
                "root_cause": "PR needs CHANGES.md entry",
                "review_decision": None,
            },
        )
        # Iteration 2 (the one the run actually terminated on): TL re-ran,
        # escalated with confidence=0.0.
        tl_2 = _task(
            role="cifix_techlead", seq=5,
            task_id="tl-iter-2",
            output={
                "confidence": 0.0,
                "root_cause": "",
                "review_decision": "ESCALATE",
            },
        )
        prov = build_provenance([tl_1, tl_2])

        # The provenance MUST point at iter-2 (highest seq).
        assert prov["tl_task_id"] == "tl-iter-2"
        assert prov["tl_task_confidence"] == 0.0
        assert prov["tl_task_review_decision"] == "ESCALATE"
        # And the existence of a previous iteration is visible via tl_task_count.
        assert prov["tl_task_count"] == 2

        # If someone had written 0.82 to the ledger (the W1.9 symptom),
        # the consistency check would flag it:
        diverged, reasons = check_consistency(
            ledger_confidence=0.82,
            ledger_root_cause="PR needs CHANGES.md entry",
            provenance=prov,
            tl_task_root_cause_full="",
        )
        assert diverged is True
        assert any("0.82" in r and "0.0" in r for r in reasons), (
            f"divergence reason must show both values; got {reasons}"
        )


# ── Schema v3: verification evidence (before/after) ───────────────────────────


class TestVerificationEvidence:
    """The before/after evidence is pure surfacing of agent-produced data."""

    def _shipped_tasks(self):
        tl = _task(
            role="cifix_techlead",
            seq=2,
            output={
                "confidence": 0.93,
                "root_cause": "unused import string triggers F401",
                "failing_command": "ruff check .",
                "error_line_quote": "F401 `string` imported but unused",
            },
        )
        verify = _task(
            role="cifix_sre_verify",
            seq=4,
            output={
                "jobs": [
                    {
                        "cmd": "ruff check scripts/x.py",
                        "exit_code": 1,
                        "stdout_tail": "F401 [*] `string` imported but unused",
                    }
                ]
            },
        )
        eng = _task(
            role="cifix_engineer",
            seq=3,
            output={"verify": {"cmd": "ruff check scripts/x.py", "exit_code": 0}},
        )
        return [tl, verify, eng]

    def test_evidence_assembled_from_agent_output(self):
        prov = build_provenance(self._shipped_tasks())
        ev = prov["verification_evidence"]
        assert ev is not None
        assert ev["failing_command"] == "ruff check ."
        assert ev["error_line"] == "F401 `string` imported but unused"
        assert ev["before"]["exit_code"] == 1
        assert ev["before"]["cmd"] == "ruff check scripts/x.py"
        assert "F401" in ev["before"]["output_tail"]
        assert ev["after"]["exit_code"] == 0
        assert ev["after"]["cmd"] == "ruff check scripts/x.py"

    def test_evidence_none_when_absent(self):
        # A run with only a bare TL task (no verify command, no jobs) → None.
        prov = build_provenance([_task(role="cifix_techlead", seq=1)])
        assert prov["verification_evidence"] is None

    def test_before_output_tail_capped(self):
        verify = _task(
            role="cifix_sre_verify",
            seq=1,
            output={"jobs": [{"cmd": "x", "exit_code": 1, "stdout_tail": "E" * 5000}]},
        )
        prov = build_provenance([verify])
        tail = prov["verification_evidence"]["before"]["output_tail"]
        assert len(tail) < 5000
        assert "truncated" in tail
