"""P0-5 — shadow ledger provenance.

Every terminal-state ledger row records exactly which task row each of
its fields was derived from. The W1.9 incident (2026-05-11 psf/black
changelog dispatch) revealed that the ledger's confidence + root_cause
could disagree with the actual cifix_techlead task output, with no way
to prove which value was correct. This module closes that audit gap.

Provenance schema (stored in shadow_ledger.phalanx_provenance JSONB):

    {
      "_schema_version": 1,
      "chosen_source_role": "cifix_techlead",
      "chosen_source_reason": "TL output is canonical source for confidence + root_cause",
      "tl_task_id": "uuid",
      "tl_task_created_at": "2026-05-11T20:15:05+00:00",
      "tl_task_sequence_num": 2,
      "tl_task_confidence": 0.82,
      "tl_task_review_decision": "ESCALATE",
      "tl_task_root_cause_head": "PR needs CHANGES.md entry",
      "tl_task_count": 2,
      "engineer_task_id": "uuid or null",
      "engineer_task_status": "FAILED",
      "engineer_task_confidence": null,
      "root_cause_synthesized": false,
      "root_cause_synthesis_reason": null,
      "divergence_detected": false,
      "divergence_details": null
    }

Invariants:
  - If chosen_source_role == "cifix_techlead", the ledger's
    phalanx_confidence/phalanx_root_cause MUST equal the recorded
    tl_task_confidence/tl_task_root_cause (modulo synthesis).
  - root_cause_synthesized=true is only valid when the chosen source
    task had no root_cause AND the verdict is SAFE_ESCALATE — in which
    case the writer synthesizes a reason from the classification.
  - divergence_detected=true means the consistency check at write time
    observed (ledger field) != (source task field). The row is still
    written so the dispatch completes; audit tools filter on this flag.
"""

from __future__ import annotations

from typing import Any

PROVENANCE_SCHEMA_VERSION = 3  # bumped: adds verification_evidence (before/after) field
SOURCE_ROLE_TECHLEAD = "cifix_techlead"
SOURCE_ROLE_ENGINEER = "cifix_engineer"
SOURCE_REASON_TL_CANONICAL = (
    "TL output is canonical source for confidence + root_cause"
)


def _iso(v) -> str | None:
    """Datetime → ISO string; None if missing."""
    if v is None:
        return None
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


def _pick_terminal_tl_task(tasks: list) -> tuple[Any | None, int]:
    """Return (chosen_TL_task, total_TL_task_count). The chosen task is
    the highest sequence_num among cifix_techlead tasks. Tie-break by
    created_at desc to be deterministic.
    """
    tl_tasks = [t for t in tasks if getattr(t, "agent_role", None) == "cifix_techlead"]
    if not tl_tasks:
        return None, 0
    # Sort by (sequence_num, created_at) descending; picking [0] gives the latest.
    tl_tasks_sorted = sorted(
        tl_tasks,
        key=lambda t: (
            getattr(t, "sequence_num", 0) or 0,
            getattr(t, "created_at", None) or 0,
        ),
        reverse=True,
    )
    return tl_tasks_sorted[0], len(tl_tasks)


def _pick_terminal_engineer_task(tasks: list) -> Any | None:
    eng_tasks = [t for t in tasks if getattr(t, "agent_role", None) == "cifix_engineer"]
    if not eng_tasks:
        return None
    eng_sorted = sorted(
        eng_tasks,
        key=lambda t: (
            getattr(t, "sequence_num", 0) or 0,
            getattr(t, "created_at", None) or 0,
        ),
        reverse=True,
    )
    return eng_sorted[0]


def _pick_terminal_verify_task(tasks: list) -> Any | None:
    """Return the latest cifix_sre_verify task (carries the before-fix
    baseline reproduction in its `jobs` array)."""
    v_tasks = [t for t in tasks if getattr(t, "agent_role", None) == "cifix_sre_verify"]
    if not v_tasks:
        return None
    v_sorted = sorted(
        v_tasks,
        key=lambda t: (
            getattr(t, "sequence_num", 0) or 0,
            getattr(t, "created_at", None) or 0,
        ),
        reverse=True,
    )
    return v_sorted[0]


def _task_output_dict(task) -> dict:
    """Safely extract the task's output as a dict."""
    if task is None:
        return {}
    out = getattr(task, "output", None)
    return out if isinstance(out, dict) else {}


# Cap the captured before-fix output so the JSONB column (and the rendered
# comment) stay small. The maintainer reads the real run in CI; this is a
# grounded excerpt, not a full log.
_EVIDENCE_OUTPUT_MAX_CHARS = 600


def _build_verification_evidence(tl_out: dict, eng_out: dict, verify_out: dict) -> dict | None:
    """Assemble the before/after verification evidence from data the agents
    already produced. Pure surfacing — no new computation, no I/O.

      - failing_command / error_line : from the TL's diagnosis
      - before                       : the sre_verify baseline reproduction
                                       (the failing command re-run in a clean
                                       sandbox; exit_code should be non-zero)
      - after                        : the engineer's post-fix re-run of the
                                       same verify command (exit_code 0 on SHIP)

    Returns None when none of the fields are present, so the renderer can
    cleanly omit the block on older/sparse rows.
    """
    failing_command = tl_out.get("failing_command")
    error_line = tl_out.get("error_line_quote")

    # before: first job in the sre_verify run is the baseline reproduction.
    before: dict | None = None
    jobs = verify_out.get("jobs")
    if isinstance(jobs, list) and jobs:
        j0 = jobs[0] if isinstance(jobs[0], dict) else {}
        out_tail = (j0.get("stdout_tail") or j0.get("stderr_tail") or "").strip()
        if len(out_tail) > _EVIDENCE_OUTPUT_MAX_CHARS:
            out_tail = out_tail[:_EVIDENCE_OUTPUT_MAX_CHARS] + "\n[... output truncated ...]"
        before = {
            "cmd": j0.get("cmd"),
            "exit_code": j0.get("exit_code"),
            "output_tail": out_tail or None,
        }

    # after: the engineer's verify result (the same command, post-fix).
    after: dict | None = None
    eng_verify = eng_out.get("verify")
    if isinstance(eng_verify, dict):
        after = {
            "cmd": eng_verify.get("cmd"),
            "exit_code": eng_verify.get("exit_code"),
        }

    if not any([failing_command, error_line, before, after]):
        return None

    return {
        "failing_command": failing_command,
        "error_line": error_line,
        "before": before,
        "after": after,
    }


def build_provenance(
    tasks: list,
    *,
    chosen_source_role: str = SOURCE_ROLE_TECHLEAD,
    chosen_source_reason: str = SOURCE_REASON_TL_CANONICAL,
    root_cause_synthesized: bool = False,
    root_cause_synthesis_reason: str | None = None,
    sre_setup_diagnostic: dict | None = None,
) -> dict[str, Any]:
    """Construct a provenance dict from the run's task rows.

    Always returns a structured dict — callers can compare its fields to
    the ledger row at write time to detect divergence.
    """
    tl_task, tl_task_count = _pick_terminal_tl_task(tasks)
    eng_task = _pick_terminal_engineer_task(tasks)
    verify_task = _pick_terminal_verify_task(tasks)

    tl_out = _task_output_dict(tl_task)
    eng_out = _task_output_dict(eng_task)
    verify_out = _task_output_dict(verify_task)

    rc_full = tl_out.get("root_cause")
    rc_head = (rc_full[:120] if isinstance(rc_full, str) else None)

    return {
        "_schema_version": PROVENANCE_SCHEMA_VERSION,
        "chosen_source_role": chosen_source_role,
        "chosen_source_reason": chosen_source_reason,
        "tl_task_id": getattr(tl_task, "id", None),
        "tl_task_created_at": _iso(getattr(tl_task, "created_at", None)),
        "tl_task_sequence_num": getattr(tl_task, "sequence_num", None),
        "tl_task_status": getattr(tl_task, "status", None),
        "tl_task_confidence": tl_out.get("confidence"),
        "tl_task_review_decision": tl_out.get("review_decision"),
        "tl_task_root_cause_head": rc_head,
        "tl_task_count": tl_task_count,
        "engineer_task_id": getattr(eng_task, "id", None),
        "engineer_task_status": getattr(eng_task, "status", None),
        "engineer_task_confidence": eng_out.get("confidence"),
        "root_cause_synthesized": root_cause_synthesized,
        "root_cause_synthesis_reason": root_cause_synthesis_reason,
        "divergence_detected": False,
        "divergence_details": None,
        # P1-6 — when failure_class=FAILED_SANDBOX_SETUP, carries the
        # structured diagnostic from the failed SRE setup task so the
        # ledger row is actionable without re-querying the DB.
        "sre_setup_diagnostic": sre_setup_diagnostic,
        # Schema v3 — before/after verification evidence surfaced from the
        # TL diagnosis + sre_verify baseline + engineer post-fix re-run.
        # Pure data surfacing for the maintainer comment; None when absent.
        "verification_evidence": _build_verification_evidence(tl_out, eng_out, verify_out),
    }


def check_consistency(
    *,
    ledger_confidence: float | None,
    ledger_root_cause: str | None,
    provenance: dict[str, Any],
    tl_task_root_cause_full: str | None,
) -> tuple[bool, list[str]]:
    """Compare the values the ledger is about to record against the values
    the provenance says they came from. Returns (divergence_detected, reasons).

    The full TL root_cause is passed separately because provenance only
    stores the head — we need the full string for byte-exact comparison.
    """
    reasons: list[str] = []
    chosen = provenance.get("chosen_source_role")

    if chosen == SOURCE_ROLE_TECHLEAD:
        tl_conf = provenance.get("tl_task_confidence")
        # Normalize None vs 0.0 etc.
        if not _confidences_match(ledger_confidence, tl_conf):
            reasons.append(
                f"ledger_confidence={ledger_confidence!r} but tl_task_confidence={tl_conf!r}"
            )
        if not provenance.get("root_cause_synthesized"):
            if not _strings_match(ledger_root_cause, tl_task_root_cause_full):
                reasons.append(
                    "ledger_root_cause does not match tl_task root_cause "
                    "(and was not flagged as synthesized)"
                )
    elif chosen == SOURCE_ROLE_ENGINEER:
        eng_conf = provenance.get("engineer_task_confidence")
        if not _confidences_match(ledger_confidence, eng_conf):
            reasons.append(
                f"ledger_confidence={ledger_confidence!r} but engineer_task_confidence={eng_conf!r}"
            )

    return (len(reasons) > 0, reasons)


def _confidences_match(a: float | None, b: float | None) -> bool:
    """Tolerant equality: both None match; numeric tolerance for floats."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def _strings_match(a: str | None, b: str | None) -> bool:
    """None == None, "" == None, otherwise exact match."""
    if not a and not b:
        return True
    return a == b


# ── Terminal-state validator (P0-6) ──────────────────────────────────────────


# tl_task_status values that indicate the TL task has not finished yet.
# A SAFE_ESCALATE verdict claiming "TL chose to escalate" requires a
# COMPLETED TL task — anything else means the snapshot was taken before
# the run's true terminal state.
_TL_NON_TERMINAL_STATUSES = frozenset({"PENDING", "IN_PROGRESS", "CANCELLED"})


def is_well_formed_terminal_state(
    *,
    verdict: str | None,
    provenance: dict | None,
) -> tuple[bool, str | None]:
    """Decide whether a (verdict, provenance) pair is safe to write as
    the terminal ledger snapshot.

    Returns (ok, reason_if_not). Callers MUST refuse to write the row
    when ok is False — the reconciler will finalize the row later when
    the run actually reaches a terminal state.

    Rules:
      - PENDING is always well-formed (it IS the pre-terminal sentinel).
      - SAFE_ESCALATE asserts "TL refused to ship". This requires a TL
        task that is in a terminal state (COMPLETED / FAILED), not one
        that's still running or was killed before completing.
      - SHIPPED_PROPOSED and FAILED can be valid with tl_task_count=0
        (e.g. FAILED_SANDBOX_SETUP — TL never ran) so we don't gate
        them on tl_task_status.
    """
    if verdict in (None, "PENDING"):
        return True, None

    prov = provenance or {}
    tl_status = prov.get("tl_task_status")
    tl_count = prov.get("tl_task_count") or 0

    if verdict == "SAFE_ESCALATE":
        # SAFE_ESCALATE without ANY TL task is fine only if the
        # provenance also says root_cause_synthesized=True (the
        # synthesizer fired because TL never produced anything). The
        # CLI's classification path already handles this case by
        # synthesizing a reason.
        if tl_count == 0:
            return True, None
        if tl_status in _TL_NON_TERMINAL_STATUSES:
            return False, (
                f"SAFE_ESCALATE requires TL terminal output but "
                f"tl_task_status={tl_status!r}; "
                "snapshot was taken before run reached terminal state"
            )
        if tl_status is None:
            return False, (
                "SAFE_ESCALATE with non-zero tl_task_count requires "
                "tl_task_status to be set; got None"
            )

    return True, None


# ── SAFE_ESCALATE empty-diagnosis synthesis ──────────────────────────────────


def synthesize_root_cause_for_safe_escalate(
    classification_reason: str | None,
    tl_output: dict | None,
) -> str:
    """When verdict is SAFE_ESCALATE and the TL output has no root_cause,
    synthesize a structured reason so the ledger row is never
    operationally indistinguishable from a silent failure.

    The classification_reason indicates which SAFE_ESCALATE sub-case
    fired (see runner._classify_verdict)."""
    tl = tl_output or {}
    review_decision = tl.get("review_decision")

    if classification_reason == "calibration_failed":
        return (
            "Phalanx escalated: TL's confidence calibration failed validation "
            "(hedged confidence on a localized deterministic fix)."
        )
    if classification_reason == "self_critique_inconsistent":
        return (
            "Phalanx escalated: TL's self-critique flagged internal "
            "inconsistency (one of c1–c8 grounding checks failed)."
        )
    if classification_reason == "tl_escalated":
        return (
            "Phalanx escalated: TL emitted review_decision='ESCALATE' "
            "without providing a root_cause."
        )
    if classification_reason == "tl_zero_confidence":
        return (
            "Phalanx escalated: TL emitted confidence=0.0 without a "
            "root_cause (canonical low-confidence escalate)."
        )
    return (
        "Phalanx escalated. Underlying TL output did not provide a "
        f"root_cause; review_decision={review_decision!r}."
    )


def synthesize_root_cause_for_sandbox_setup(diagnostic: dict) -> str:
    """P1-6 — build a one-line operator-actionable root_cause from the
    structured SRE setup diagnostic. The ledger row alone tells you
    which phase + command failed, with what exit code, and what stderr.
    No DB drill-down required.

    P1-6 v2: includes phase, failed_command, exit_code, and stderr tail.
    """
    phase = diagnostic.get("phase") or "unknown"
    step = diagnostic.get("failed_step") or "unknown"
    cmd = diagnostic.get("failed_command") or step
    exit_code = diagnostic.get("exit_code")
    stderr = (diagnostic.get("stderr_tail") or diagnostic.get("error_message") or "").strip()
    image = diagnostic.get("base_image") or "?"

    parts = [
        f"Sandbox bootstrap failed at step={step!r} (phase={phase})",
        f"on base_image={image}",
    ]
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    cmd_trim = cmd if len(cmd) <= 80 else cmd[:77] + "..."
    parts.append(f"cmd={cmd_trim!r}")
    if stderr:
        stderr_trim = stderr if len(stderr) <= 200 else stderr[:197] + "..."
        parts.append(f"stderr={stderr_trim!r}")
    return " | ".join(parts)[:600]
