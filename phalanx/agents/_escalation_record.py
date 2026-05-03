"""v1.7.2.3 — Structured escalation record built when a CI fix run terminates
non-green (FAILED, ESCALATED, cost_cap, runtime_cap, no_progress, etc.).

The record is the single source of truth a human picks up when the bot
escalates. It's stored on `runs.metadata.escalation_record` (JSON) and
optionally surfaced in the PR comment.

Shape:

  {
    "final_reason": "no_progress_detected" | "iterations_exhausted" | ...,
    "iterations": [
      {
        "iter": 1,
        "tl": {
          "root_cause": "...",
          "confidence": 0.94,
          "verify_command": "ruff check src/x.py",
          "open_questions": [...]
        },
        "engineer": {
          "committed": true,
          "commit_sha": "abc1234...",
          "skipped_reason": null,
          "patch_safety_violation": null
        },
        "challenger": {"verdict": "accept", "objections": []},
        "verify": {
          "verdict": "new_failures",
          "exit_code": 1,
          "fingerprint": "deadbeef12345678",
          "stdout_tail": "...",
          "stderr_tail": "...",
          "engineer_commit_sha": "abc1234...",
          "verified_commit_sha": "abc1234..."
        }
      },
      ...
    ]
  }

This is built deterministically from task rows — no agent invocation, no
LLM. The commander writes it on terminal failure.
"""

from __future__ import annotations

from typing import Any


def build_escalation_record(
    *, final_reason: str, tasks: list[dict]
) -> dict[str, Any]:
    """Walk completed tasks for one run and produce the iteration ledger.

    `tasks` is a list of dicts with keys: sequence_num, agent_role,
    status, output, error. Order does not matter — we sort by sequence_num.

    Iterations are inferred by walking sre_verify task numbers in order:
    the iter-N sre_verify is the N-th in the list. TL/engineer/challenger
    that precede it (by sequence_num, after iter-(N-1)'s sre_verify) are
    grouped into iter-N's record.
    """
    sorted_tasks = sorted(tasks, key=lambda t: t.get("sequence_num", 0))

    iterations: list[dict] = []
    pending: dict[str, dict | None] = {
        "tl": None,
        "engineer": None,
        "challenger": None,
        "verify": None,
    }
    iter_num = 0

    def _flush() -> None:
        nonlocal iter_num
        # Only emit an iteration record if a verify actually happened
        # OR a tl/engineer ran without verify (escalation mid-iter).
        if not any(pending.values()):
            return
        iter_num += 1
        iterations.append({
            "iter": iter_num,
            "tl": _summarize_tl(pending["tl"]),
            "engineer": _summarize_engineer(pending["engineer"]),
            "challenger": _summarize_challenger(pending["challenger"]),
            "verify": _summarize_verify(pending["verify"]),
        })
        for k in pending:
            pending[k] = None

    for t in sorted_tasks:
        role = t.get("agent_role") or ""
        if t.get("status") != "COMPLETED" and t.get("status") != "FAILED":
            continue
        output = t.get("output") if isinstance(t.get("output"), dict) else None

        if role == "cifix_techlead":
            pending["tl"] = output
        elif role == "cifix_engineer":
            pending["engineer"] = output or {"error": t.get("error")}
        elif role == "cifix_challenger":
            pending["challenger"] = output
        elif role in {"cifix_sre", "cifix_sre_verify"}:
            if output and output.get("mode") == "verify":
                pending["verify"] = output
                _flush()
            # sre_setup outputs are infrastructure — skip in the ledger

    # Flush any trailing incomplete iteration (e.g. engineer aborted on
    # low-confidence with no verify ran)
    if any(pending.values()):
        _flush()

    return {
        "final_reason": final_reason,
        "iterations": iterations,
        "n_iterations": len(iterations),
    }


def _summarize_tl(tl: dict | None) -> dict | None:
    if not tl:
        return None
    return {
        "root_cause": tl.get("root_cause"),
        "fix_spec": tl.get("fix_spec"),
        "affected_files": tl.get("affected_files"),
        "verify_command": tl.get("verify_command"),
        "verify_success": tl.get("verify_success"),
        "confidence": tl.get("confidence"),
        "open_questions": tl.get("open_questions"),
        "review_decision": tl.get("review_decision"),
    }


def _summarize_engineer(eng: dict | None) -> dict | None:
    if not eng:
        return None
    out = {
        "committed": eng.get("committed"),
        "commit_sha": eng.get("commit_sha"),
        "files_modified": eng.get("files_modified"),
        "skipped_reason": eng.get("skipped_reason"),
        "v17_path": eng.get("v17_path"),
    }
    # Surface a patch-safety block prominently in escalation
    failed_step = eng.get("failed_step")
    if failed_step and isinstance(failed_step, dict):
        err = failed_step.get("error") or ""
        if err.startswith("patch_safety_violation"):
            out["patch_safety_violation"] = {
                "rule": err.split(":", 1)[1] if ":" in err else err,
                "detail": failed_step.get("detail"),
            }
    return out


def _summarize_challenger(ch: dict | None) -> dict | None:
    if not ch:
        return None
    return {
        "verdict": ch.get("verdict"),
        "objections": ch.get("objections"),
        "shadow_mode": ch.get("shadow_mode"),
    }


def _summarize_verify(verify: dict | None) -> dict | None:
    if not verify:
        return None
    new_failures = verify.get("new_failures") or []
    first = new_failures[0] if new_failures else {}
    return {
        "verdict": verify.get("verdict"),
        "verify_scope": verify.get("verify_scope"),
        "verify_command": verify.get("verify_command"),
        "fingerprint": verify.get("fingerprint"),
        "engineer_commit_sha": verify.get("engineer_commit_sha"),
        "verified_commit_sha": verify.get("verified_commit_sha"),
        "sandbox_sync": verify.get("sandbox_sync"),
        "exit_code": first.get("exit_code"),
        "stdout_tail": first.get("stdout_tail"),
        "stderr_tail": first.get("stderr_tail"),
    }


__all__ = ["build_escalation_record"]
