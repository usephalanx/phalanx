# Migration bootstrap CI (P0-4)

Every PR that touches migrations or db models must prove a fresh
postgres can be brought up from zero. This catches the 2026-03-21 class
of latent bug — where a migration "works" in production only because
its target table already existed from earlier dev sessions.

## What the CI workflow does

[.github/workflows/migration-bootstrap.yml](../../.github/workflows/migration-bootstrap.yml)
spins up a fresh `pgvector/pgvector:pg16` service on every PR that
touches:

- `alembic/**`
- `phalanx/db/**`
- The workflow file or `scripts/migration_bootstrap_check.sh` itself

It then runs, in order:

| Step | Command                                       | Catches                                                 |
| ---- | --------------------------------------------- | ------------------------------------------------------- |
| 1    | `alembic upgrade head` (from empty)           | Type mismatches, missing extensions, ordering bugs     |
| —    | Capture schema fingerprint                    | Per-column shape, sorted, stable                        |
| 2    | `alembic downgrade base`                      | One-way migrations, missing downgrade() bodies          |
| —    | Assert zero application tables remain         | Half-done downgrades                                    |
| 3    | `alembic upgrade head` (round-trip)           | State-dependent migrations                              |
| —    | Capture schema fingerprint, diff vs Step 1    | Upgrade/downgrade asymmetry                             |

Any step failure fails the job. The fingerprints are uploaded as
artifacts (`if: always()`) so failed runs are debuggable from the PR.

## Run the same check locally

```bash
make migration-check
```

This invokes [scripts/migration_bootstrap_check.sh](../../scripts/migration_bootstrap_check.sh),
which:

- Starts a throwaway postgres on a random port (no collision with `forge-postgres`)
- Runs the same three alembic commands
- Captures and diffs the same fingerprint
- Tears down the throwaway container on exit (success or failure)

Expected output on a healthy migration chain:

```
→ Step 1: alembic upgrade head (fresh DB)...
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema ...
...
→ capturing fresh-install fingerprint...
    472 columns observed
→ Step 2: alembic downgrade base...
→ Step 3: alembic upgrade head (round-trip)...
→ capturing round-trip fingerprint...

✅ migration-bootstrap-check passed
   columns:          472
   fingerprints:     /tmp/fingerprint-fresh.txt  /tmp/fingerprint-roundtrip.txt
```

If the round-trip fingerprint diff is non-empty, the script prints
the diff and exits 1. The two `/tmp/fingerprint-*.txt` files are left
behind for inspection.

## Authoring a new migration: the local-first workflow

1. Write the migration: `make migrate-new m=your_description`
2. Edit the generated file in `alembic/versions/`
3. Run `make migration-check` — this MUST pass before opening the PR
4. Open the PR — CI runs the same check automatically

If the check fails locally, fix the migration. Common failure shapes:

- **FK type mismatch**: `sa.String()` on a column referencing a `UUID` parent (the 2026-03-21 bug). Fix: use `UUID(as_uuid=False)` on both sides.
- **Missing extension**: a migration uses `pgvector` types but doesn't have `CREATE EXTENSION IF NOT EXISTS "vector"`. Fix: add it to the initial setup of the migration that needs it.
- **Empty downgrade**: a migration adds a column but `downgrade()` is `pass`. Fix: write the matching `op.drop_column`.
- **State-dependent upgrade**: a migration reads existing data (e.g. `for row in connection.execute("SELECT ...")`) and fails when the DB is empty. Fix: guard the data read with `if row_count > 0`.

## Regression proof: would this have caught the 2026-03-21 bug?

The 2026-05-11 incident was triggered by [alembic/versions/20260321_0001_add_dag_columns.py](../../alembic/versions/20260321_0001_add_dag_columns.py)
declaring `task_dependencies.task_id` as `sa.String()` against a
`tasks.id` of UUID type. The FK created on a fresh DB would have failed
with:

```
asyncpg.exceptions.DatatypeMismatchError: foreign key constraint
"task_dependencies_task_id_fkey" cannot be implemented
DETAIL: Key columns "task_id" and "id" are of incompatible types:
        character varying and uuid.
```

The proof script [scripts/migration_regression_proof.sh](../../scripts/migration_regression_proof.sh)
demonstrates this end-to-end:

1. Verifies a clean tree on the target migration file.
2. Reverts the file to the broken `sa.String()` FK columns.
3. Runs `make migration-check`.
4. Asserts the check exits non-zero with the exact `DatatypeMismatchError`.
5. Restores the file from git on script exit (success or failure).

To prove the safety net is intact at any time:

```bash
# Make sure the target migration file is clean in git (no uncommitted changes)
git status alembic/versions/20260321_0001_add_dag_columns.py

# Run the proof
./scripts/migration_regression_proof.sh
```

Expected output:

```
→ reverting UUID→String fix to recreate the 2026-03-21 bug...
   reverted; diff shows broken state:
 alembic/versions/20260321_0001_add_dag_columns.py | 7 ++++---

→ running make migration-check (expecting FAILURE)...
... (alembic upgrade fails with DatatypeMismatchError) ...

✅ regression-proof PASSED
   The check correctly refused the known-bad migration with:
asyncpg.exceptions.DatatypeMismatchError: foreign key constraint ...
DETAIL:  Key columns "task_id" and "id" are of incompatible types: ...

→ restoring alembic/versions/20260321_0001_add_dag_columns.py from git...
   restored OK
```

If the proof script reports `REGRESSION SAFETY NET BROKEN`, the
migration check has regressed and P0-4 no longer protects against
this class of bug. Investigate the workflow + the local script.

## Schema fingerprint format

The fingerprint is the output of [scripts/migration_schema_fingerprint.sh](../../scripts/migration_schema_fingerprint.sh) —
one line per column, pipe-delimited:

```
table_name|column_name|data_type|char_max|is_nullable|column_default
```

Properties:
- Excludes `alembic_version` (that table tracks the migration head and
  legitimately differs between phases).
- Ordered by `(table_name, column_name)` so the fingerprint is stable
  across runs.
- Uses `information_schema.columns` so the output matches what any
  standard tool would see.

This is intentionally simple. It catches every shape of schema drift
that affects column-level data layout. It does NOT catch:

- Index changes (no `pg_indexes` join — keeps the fingerprint readable)
- Constraint name changes (sometimes autogenerated, would create noise)
- Sequence value drift (not a schema concern)

If a future migration adds an index that matters for correctness,
a dedicated test for that index is the right answer — not bloating
the fingerprint.

## Operational risks still remaining

P0-4 closes the bootstrap-safety gap. It doesn't solve:

1. **Index/constraint drift not surfaced.** The fingerprint covers columns only. A migration that creates an index on upgrade but doesn't drop it on downgrade leaves a residual index after `downgrade base` — the script won't flag it. Mitigation: per-feature tests on indexes that matter.
2. **Data migrations are not tested.** This check operates on schema only. A migration that backfills data could be syntactically clean and behaviorally broken. P2 candidate: a separate data-migration test harness.
3. **CI runs against `pgvector/pgvector:pg16` only.** If we ever switch postgres major versions, the check would need a matrix. Today, parity with the compose image is what matters.
4. **CI does not run on changes to `phalanx/db/session.py` or settings**, only the migrations + models. A change to `session.py` that breaks engine creation would not trigger this job. Other CI gates catch that, but it's worth tracking.
5. **The regression proof script modifies a committed file in-place.** It restores via `git checkout`, but a SIGKILL between revert and restore would leave the file in the broken state. Operator awareness — documented above.
6. **Local check requires Docker.** Operators without Docker (rare on this stack) can't run `make migration-check`. CI runs in a Docker-equipped environment, so the gate still works on PRs — but the local pre-push check is unavailable.
