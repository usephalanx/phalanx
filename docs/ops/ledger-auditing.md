# Auditing shadow ledger evidence (P0-5)

Every terminal `shadow_ledger` row records exactly which task row each
of its fields was derived from. This is the audit trail that closes
the W1.9 blocker: the ledger is no longer a black box you have to take
on faith.

## What gets recorded

`shadow_ledger.phalanx_provenance` (JSONB) is populated on every
terminal write. Schema:

```json
{
  "_schema_version": 1,
  "chosen_source_role": "cifix_techlead",
  "chosen_source_reason": "TL output is canonical source for confidence + root_cause",
  "tl_task_id": "8249a9f9-8b25-4e1c-af41-1515bad59a5a",
  "tl_task_created_at": "2026-05-11T20:15:05+00:00",
  "tl_task_sequence_num": 2,
  "tl_task_status": "COMPLETED",
  "tl_task_confidence": 0.82,
  "tl_task_review_decision": null,
  "tl_task_root_cause_head": "PR needs CHANGES.md entry",
  "tl_task_count": 1,
  "engineer_task_id": "0b5c...",
  "engineer_task_status": "FAILED",
  "engineer_task_confidence": null,
  "root_cause_synthesized": false,
  "root_cause_synthesis_reason": null,
  "divergence_detected": false,
  "divergence_details": null
}
```

## How ledger evidence is derived

The dispatch finalization path
([phalanx/shadow/runner.py](../../phalanx/shadow/runner.py))
does the following, in order:

1. `_classify_verdict(...)` returns `(verdict, classification_reason)`.
2. Reads `tl.get("confidence")` and `tl.get("root_cause")` from the
   terminal-state TL task. The TL task is selected as the highest
   `sequence_num` `cifix_techlead` row (tie-break: `created_at desc`).
3. If verdict is `SAFE_ESCALATE` and `root_cause` is empty/null:
   - synthesizes a root_cause string from the `classification_reason`
     (e.g. "Phalanx escalated: TL emitted confidence=0.0 without a
     root_cause"). This closes the W1.10 blind spot — SAFE_ESCALATE
     rows can never have NULL `phalanx_root_cause`.
4. `build_provenance(tasks, ...)` constructs the JSONB payload above.
5. `check_consistency(...)` compares the values about to be written
   against the source task. If they disagree:
   - `provenance.divergence_detected = true`
   - `provenance.divergence_details` lists the specific mismatches
   - A structured `ledger.divergence` event is logged
   - The row is still written (the dispatch must not be blocked)
6. `update_with_results(..., provenance=provenance)` commits the row +
   appends the JSONL line (P0-2).

## What "no silent fallback" means

`chosen_source_role` is **always `cifix_techlead`** for today's code.
If a future change ever routes confidence or root_cause from the
engineer (or any other agent) instead, the code MUST set this field to
`cifix_engineer` and populate `chosen_source_reason`. The architecture
will not let confidence quietly come from a different agent than the
provenance claims.

The engineer's task is recorded in provenance (`engineer_task_id`,
`engineer_task_confidence`) regardless — so cross-reference is always
available — but the engineer's confidence is NEVER promoted to the
ledger's `phalanx_confidence` field without an explicit `chosen_source_role`
change.

## How to audit a ledger row against tasks

Pick a row and confirm it matches the task it claims to derive from:

```bash
LEDGER_ID="06a827ea-c3e1-4ade-ac7a-c7a627087b4c"

# Pull the ledger row + provenance.
docker exec forge-postgres psql -U forge -d forge -A -t -F'|' -c "
  SELECT phalanx_confidence,
         phalanx_root_cause,
         phalanx_provenance->>'tl_task_id'         AS tl_task_id,
         phalanx_provenance->>'tl_task_confidence' AS prov_conf,
         phalanx_provenance->>'tl_task_count'      AS tl_count,
         phalanx_provenance->>'divergence_detected' AS diverged
    FROM shadow_ledger
   WHERE id = '${LEDGER_ID}';
"

# Now pull the underlying TL task.
TL_TASK_ID="$(docker exec forge-postgres psql -U forge -d forge -A -t -c \
  "SELECT phalanx_provenance->>'tl_task_id' FROM shadow_ledger WHERE id='${LEDGER_ID}';")"

docker exec forge-postgres psql -U forge -d forge -A -t -F'|' -c "
  SELECT id,
         agent_role,
         sequence_num,
         status,
         output->>'confidence'      AS confidence,
         output->>'review_decision' AS review_decision,
         left(output->>'root_cause', 80) AS root_cause_head
    FROM tasks
   WHERE id = '${TL_TASK_ID}';
"
```

The two `confidence` values **must match**. If they don't, look at
`divergence_detected` in the provenance — it should be `true` and
`divergence_details` will explain what diverged. If `divergence_detected`
is `false` but the values still don't match, that's a bug in the
consistency check itself.

## CLI tools

```bash
# Show a ledger row with provenance summary
.venv/bin/python -m phalanx.shadow.cli show <ledger_id>

# Export the whole ledger to JSON (provenance included)
.venv/bin/python -m phalanx.shadow.cli export /tmp/ledger.json

# Quick stats on the JSONL stream (P0-2 + provenance)
make ledger-stats
make ledger-tail
```

The `run` subcommand also prints the provenance summary in its banner:

```
✅ Shadow run complete — verdict: SHIPPED_PROPOSED
------------------------------------------------------------
  ...
  provenance:
    source       : cifix_techlead
    tl_task_id   : 8249a9f9-...
    tl_seq#      : 2
    tl_count     : 1
    tl_confidence: 0.9
    review       : None
```

## Divergence checks

The consistency check fires at write time. Rules:

| Field                 | Must equal                                                              |
| --------------------- | ----------------------------------------------------------------------- |
| `phalanx_confidence`  | `provenance.tl_task_confidence` (when source role is `cifix_techlead`)  |
| `phalanx_root_cause`  | The chosen task's full `root_cause` — unless `root_cause_synthesized=true`, in which case the synthesizer's output is canonical |

Tolerances:
- Confidences use a 1e-9 absolute tolerance for floating-point.
- Empty string and `None` for root_cause are treated as equal.

When divergence is detected, the row is **still written** (the dispatch
must not be blocked on an audit-layer disagreement) but:

- `provenance.divergence_detected = true`
- `provenance.divergence_details = [reasons...]`
- structlog event `ledger.divergence` is logged with the full context

Audit tools should filter on `divergence_detected` and investigate
each row individually. There should be zero of these in a healthy week.

## SAFE_ESCALATE empty-diagnosis synthesis

When a run lands as SAFE_ESCALATE but the TL output has no `root_cause`,
the writer synthesizes one based on which classification sub-case
fired. The four classification reasons:

| Reason                            | Synthesized text                                                       |
| --------------------------------- | ---------------------------------------------------------------------- |
| `tl_escalated`                    | "Phalanx escalated: TL emitted review_decision='ESCALATE' without providing a root_cause." |
| `tl_zero_confidence`              | "Phalanx escalated: TL emitted confidence=0.0 without a root_cause."   |
| `calibration_failed`              | "Phalanx escalated: TL's confidence calibration failed validation."    |
| `self_critique_inconsistent`      | "Phalanx escalated: TL's self-critique flagged internal inconsistency." |

`provenance.root_cause_synthesized=true` records that the ledger's
`phalanx_root_cause` was synthesized, not pulled from the TL output.
`provenance.root_cause_synthesis_reason` records which sub-case fired.

## W1.9 — historical row vs. forward fix

The original W1.9 row (psf/black changelog, workflow_run_id=25407733985,
2026-05-07) is **gone**. The DB volume was destroyed on 2026-05-11
when `docker compose up` silently mounted a fresh volume. P0-1 + P0-3
prevent that class of incident going forward.

Because the W1.9 task rows are unrecoverable, the forward fix is
proven against a fixture that synthesizes the multi-iteration shape:

- See [tests/unit/shadow/test_provenance.py::TestW19ForwardFixReproduction](../../tests/unit/shadow/test_provenance.py).
- Two `cifix_techlead` tasks with diverging confidences (0.82 and 0.0).
- Provenance correctly identifies iter-2 (highest sequence) as the
  source, and if a hypothetical writer recorded `0.82` to the ledger
  (the W1.9 shape), the consistency check would flag it with a
  divergence reason naming both values.

## Operational limitations

P0-5 makes the ledger auditable. It does not (yet) solve:

1. **No automated daily audit job.** An operator must run the SQL
   queries above to spot-check rows. P2 candidate: a nightly task that
   counts `divergence_detected=true` rows and alerts on > 0.
2. **Provenance only covers TL + engineer.** If a future agent's output
   ever flows into ledger fields, the provenance schema needs a new
   slot (bump `_schema_version`).
3. **Synthesized root_causes are not graded for quality.** They're
   structurally correct (always say *why* Phalanx escalated) but the
   sentence is fixed text per classification sub-case. Audit tools
   should not treat a synthesized root_cause as evidence of TL's
   actual reasoning — only as a marker that TL refused to ship.
4. **The consistency check runs at write time only.** A retroactive
   mutation of `Task.output` (which shouldn't happen, but isn't
   structurally prevented) would not re-trigger the check. The
   provenance stays pinned to the values that existed at write time —
   which is the correct audit behavior.
5. **No on-disk schema migration tool for old rows.** Pre-P0-5 ledger
   rows have `phalanx_provenance=NULL`. Audit tooling must tolerate
   this; we don't backfill historical rows because we don't have the
   source task data to derive accurate provenance from.
