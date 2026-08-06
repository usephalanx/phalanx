# Ledger reconciliation (P0-6)

The shadow ledger is now **eventually consistent** with the underlying
run state. This closes the W2 Batch 1 evidence-corruption hole.

## What changed (TL;DR)

1. The CLI no longer writes a terminal verdict when the run is still
   non-terminal. Instead it exits with `_cli_status=RUN_STILL_ACTIVE`
   and leaves the ledger row PENDING.
2. The CLI runs a new validator before writing any terminal verdict:
   `is_well_formed_terminal_state`. SAFE_ESCALATE with a non-terminal
   TL task is **refused**; the ledger row stays PENDING.
3. A new beat-scheduled task `reconcile_shadow_ledger` runs every 120s
   on `forge-worker`. It heals PENDING rows whose runs have reached a
   terminal state, and replaces ill-formed terminal snapshots with the
   current truth.

## Lifecycle

```
T0       CLI: dispatch → ledger PENDING
T1       CLI: link phalanx_run_id
T2-T*    CLI: poll Run.status (bounded wait)
            ├── terminal within wait + well-formed → write terminal row
            ├── terminal within wait but ill-formed → REFUSE, stay PENDING
            └── still EXECUTING at wait expiry → exit RUN_STILL_ACTIVE, stay PENDING
T_run    Run reaches true terminal (commander finishes / watchdog kills)
T_beat   reconcile_shadow_ledger task fires (every 120s):
            ├── finds PENDING ledger rows linked to terminal runs → heals
            ├── finds ill-formed terminal rows where snapshot pre-dates run terminal → heals
            └── leaves correctly-terminal rows alone (idempotent)
```

## Schema

Four nullable columns on `shadow_ledger`, added by
`alembic/versions/20260513_0001_shadow_ledger_reconciliation.py`:

| Column                   | Type                       | Meaning                                              |
| ------------------------ | -------------------------- | ---------------------------------------------------- |
| `reconciled_at`          | TIMESTAMP WITH TIME ZONE   | when the reconciler last healed this row             |
| `reconciled_reason`      | VARCHAR(80)                | machine-readable reason for the heal                 |
| `previous_verdict`       | VARCHAR(40)                | the verdict before reconciliation (for audit)        |
| `previous_failure_class` | VARCHAR(40)                | the failure_class before reconciliation (for audit)  |

All NULL on rows that have never been reconciled (the common case).

## Reconciliation reasons

| `reconciled_reason`                         | What it means                                                                |
| ------------------------------------------- | ---------------------------------------------------------------------------- |
| `cli_left_pending_run_terminal`              | CLI exited with RUN_STILL_ACTIVE; reconciler picked up the finalized run.    |
| `watchdog_marked_failed_infra_timeout`       | Run was alive when CLI snapshotted, watchdog later killed it.                |
| `watchdog_marked_failed_infra_worker_hang`   | Similar shape from worker-hang detector.                                     |
| `ill_formed_snapshot_replaced_X_to_Y`        | Original verdict X was ill-formed (e.g. SAFE_ESCALATE with PENDING TL).      |
| `snapshot_evolved_with_run_state`             | failure_class or other state changed on the run after the CLI snapshot.      |

The `previous_verdict` column preserves the original CLI snapshot.

## CLI semantics

The CLI now has three terminal states:

| `_cli_status`                  | When it fires                                                              | What you should do                                       |
| ------------------------------ | -------------------------------------------------------------------------- | -------------------------------------------------------- |
| (absent, normal verdict written) | Run reached terminal AND snapshot was well-formed                          | Read the verdict normally                                |
| `RUN_STILL_ACTIVE`             | CLI poll budget elapsed but run is still EXECUTING / INTAKE / TIMEOUT      | Re-check ledger later: `phalanx shadow show <ledger_id>` |
| `ILL_FORMED_SNAPSHOT_REFUSED`  | Snapshot was rejected by the validator (e.g. SAFE_ESCALATE + TL PENDING)   | Same — re-check later. Reconciler heals it.              |

Operator scripts that previously assumed `phalanx_verdict != "PENDING"`
on CLI exit must now handle these new statuses or poll the ledger.

## Operator commands

```bash
# List rows that have been reconciled
docker exec forge-postgres psql -U forge -d forge -A -F'|' -c "
  SELECT id, repo, phalanx_verdict, previous_verdict, reconciled_reason, reconciled_at
    FROM shadow_ledger
    WHERE reconciled_at IS NOT NULL
    ORDER BY reconciled_at DESC LIMIT 20;
"

# Manually trigger a reconciliation cycle (normally beat-scheduled every 120s)
docker exec forge-worker celery -A phalanx.queue.celery_app call \
  phalanx.maintenance.ledger_reconciler.reconcile_shadow_ledger

# Inspect a healed row's full state including provenance
make ledger-tail   # shows last 20 entries including reconciled ones
```

## Validator rules

`is_well_formed_terminal_state(verdict, provenance)` returns `(ok, reason)`:

| Verdict          | tl_task_count | tl_task_status                | Result                              |
| ---------------- | ------------- | ----------------------------- | ----------------------------------- |
| PENDING          | any           | any                            | OK (PENDING is valid pre-terminal)  |
| SAFE_ESCALATE    | 0             | any                            | OK (synthesized root_cause path)    |
| SAFE_ESCALATE    | >0            | PENDING / IN_PROGRESS / CANCELLED | **REJECT** (W1.9 / W2 Batch 1 shape) |
| SAFE_ESCALATE    | >0            | None                           | **REJECT** (incomplete snapshot)    |
| SAFE_ESCALATE    | >0            | COMPLETED / FAILED             | OK                                  |
| FAILED           | any           | any                            | OK (FAILED_SANDBOX_SETUP allows TL=PENDING) |
| SHIPPED_PROPOSED | any           | any                            | OK                                  |

A REJECT outcome means the CLI refuses to write the terminal row, and
the reconciler will finalize it later.

## Tests

- [tests/unit/shadow/test_reconciliation.py](../../tests/unit/shadow/test_reconciliation.py) — 16 unit tests covering the validator, decision logic, idempotency, and the W1.9 / W2 Batch 1 refusal shapes.
- The full shadow + ops suite (146 tests) remains green.

## Migration risk

Low. Four nullable additive columns, no FK constraints, no indexes.

- `alembic upgrade head` adds the columns without rewriting any rows.
- `alembic downgrade -1` drops them cleanly.
- Pre-P0-6 ledger rows have all four columns NULL — readers tolerate this.
- P0-4's migration-bootstrap CI verifies the round-trip on every PR.

## Rollback plan

If reconciliation is causing problems:

1. **Disable the beat schedule entry** in `phalanx/queue/celery_app.py` —
   remove the `"reconcile-shadow-ledger"` block; restart `forge-beat`.
2. **Revert CLI runner changes** if needed: `git revert <P0-6-commit>`.
3. **Drop columns** (optional, destructive): `alembic downgrade -1`.

Steps 1 + 2 are safe and reversible. Step 3 destroys the audit-trail
fields on already-reconciled rows; leave them as NULL is fine.

## Proof of the heal

Pre-P0-6 (W2 Batch 1, 2026-05-12): 8 ledger rows recorded
`phalanx_verdict=SAFE_ESCALATE` while their linked runs were
`status=FAILED, failure_class=FAILED_INFRA_TIMEOUT`. The ledger was
provably wrong.

Post-P0-6 (2026-05-13):

```
repo                 | phalanx_verdict | failure_class         | previous_verdict | reconciled_reason
---------------------+-----------------+-----------------------+------------------+--------------------------------------
python/mypy          | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
python/mypy          | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
python/mypy          | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
pytest-dev/pytest    | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
encode/httpx         | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
python-attrs/attrs   | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
python/mypy          | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
pytest-dev/pytest    | FAILED          | FAILED_INFRA_TIMEOUT  | SAFE_ESCALATE    | watchdog_marked_failed_infra_timeout
```

Second invocation: `{'candidates': 0, 'reconciled': 0, 'no_op': 0}` —
idempotent.

## Operational limitations still present

1. **Reconciler only heals rows whose linked run reached terminal.** A
   run stuck in EXECUTING indefinitely (no watchdog) would leave the
   ledger PENDING. The stuck-task detector remains the safeguard.
2. **No alerting on long-PENDING ledger rows.** P2 candidate: a beat
   task that warns if any row has been PENDING for >2 hours.
3. **Reconciler can't recover information that doesn't exist in the
   DB.** If the linked run was deleted, there's no source of truth.
   Pre-P0-1 backups are the recovery path.
4. **Validator only gates SAFE_ESCALATE.** FAILED and SHIPPED_PROPOSED
   pass with various tl_task_status values because those paths have
   legitimate non-TL terminal shapes. If FAILED ever becomes
   corruption-prone, extend the validator.
5. **No cap on reconciliation cycle duration.** Worst case: many stale
   rows + slow DB. Beat schedule is 120s but `soft_time_limit=240s`
   on the task — if it can't finish, it'll be killed and resume next
   cycle. Acceptable degradation.
