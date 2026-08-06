# Sandbox observability (P1-6)

Every `FAILED_SANDBOX_SETUP` ledger row is now **actionable from the row
alone**. No DB drill-down, no log search, no re-running the dispatch —
the row tells you which step failed and why.

## What changed

Before P1-6, a `FAILED_SANDBOX_SETUP` row looked like:

```
phalanx_verdict      : FAILED
failure_class        : FAILED_SANDBOX_SETUP
phalanx_root_cause   : null    ← totally opaque
```

After P1-6:

```
phalanx_verdict      : FAILED
failure_class        : FAILED_SANDBOX_SETUP
phalanx_root_cause   : Sandbox bootstrap failed at step 'setup' on
                       base_image=python:3.12-slim: docker_run_failed:
                       permission denied while trying to connect to the
                       docker API at unix:///var/run/docker.sock
                       (first install: '# requires python 3.11 ...')

provenance.sre_setup_diagnostic:
  failed_step       : setup
  error_message     : docker_run_failed: permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
  base_image        : python:3.12-slim
  stack             : python
  install_commands  : ['# requires python 3.11 (provisioner should ensure)', './misc/trigger_wheel_build.sh']
  setup_log_tail    : []
  sre_task_id       : <uuid>      ← cross-reference to the source task
```

Reading the row tells you the exact thing to fix.

## Schema

Provenance schema bumped from v1 → v2. The new field `sre_setup_diagnostic`
lives inside `shadow_ledger.phalanx_provenance` (JSONB), so there are
no new DB columns.

| Field                | Type      | Meaning                                                              |
| -------------------- | --------- | -------------------------------------------------------------------- |
| `failed_step`        | string    | pipeline step name (`docker_run_failed`, `apt_install_baseline`, `install_command`, …)  |
| `failed_command`     | string    | exact command that failed, or implicit step name when no cmd        |
| `exit_code`          | int\|null | command exit code from the failing step (null when pre-install)     |
| `phase`              | string    | derived from cmd: `apt`/`pip`/`uv`/`git`/`infra`/`unknown`           |
| `failure_subclass`   | string    | one of `FAILED_SANDBOX_SETUP_{APT,PIP,UV,GIT,UNKNOWN}`               |
| `error_message`      | string    | stripped error text from `task.error` (max 500 chars)                |
| `stderr_tail`        | string    | last 500 chars of stderr from the failing step                       |
| `stdout_tail`        | string    | last 500 chars of stdout from the failing step (often null)          |
| `base_image`         | string    | the docker base image that was being provisioned                     |
| `stack`              | string    | the env stack (e.g. `"python"`)                                       |
| `install_commands`   | list[str] | first 3 install commands from the env_spec                            |
| `setup_log_tail`     | list[dict]| last 3 setup_log entries (full per-step records)                     |
| `sre_task_id`        | uuid str  | the source SRE task ID (cross-reference for audit)                    |

**Subclassed `failure_class` (P1-6 v2):** new rows now emit one of the
five `FAILED_SANDBOX_SETUP_*` subclasses based on the failing command's
phase. The bare `FAILED_SANDBOX_SETUP` constant is retained only for
backward-compat queries (old rows still carry it). To match all sandbox
failures regardless of vintage:

```sql
WHERE failure_class LIKE 'FAILED_SANDBOX_SETUP%'
```

`sre_setup_diagnostic` is `null` when:
- The run succeeded
- The run failed for non-sandbox reasons (TL escalation, engineer failure, infra timeout, etc.)
- The SRE task FAILED with a non-`sandbox_provisioning_failed` error (e.g. `sre_blocked`)

## How to read a P1-6 row

```bash
# Operator: pull a ledger row's diagnostic
docker exec forge-postgres psql -U forge -d forge -A -F'|' -c "
  SELECT
    repo,
    phalanx_verdict,
    failure_class,
    phalanx_provenance->'sre_setup_diagnostic'->>'failed_step' AS step,
    phalanx_provenance->'sre_setup_diagnostic'->>'error_message' AS err,
    phalanx_provenance->'sre_setup_diagnostic'->>'base_image' AS image,
    LEFT(phalanx_root_cause, 100) AS rc
  FROM shadow_ledger
  WHERE failure_class = 'FAILED_SANDBOX_SETUP'
  ORDER BY created_at DESC LIMIT 10;
"
```

Or via the CLI:

```bash
.venv/bin/python -m phalanx.shadow.cli show <ledger_id>
```

The banner prints a dedicated `sre_setup_diagnostic` section when present.

## What this enables

Group ledger rows by error_message to find recurring sandbox failures:

```sql
SELECT
  phalanx_provenance->'sre_setup_diagnostic'->>'error_message' AS err,
  COUNT(*) AS n,
  array_agg(DISTINCT repo) AS repos
FROM shadow_ledger
WHERE failure_class = 'FAILED_SANDBOX_SETUP'
GROUP BY err
ORDER BY n DESC;
```

Today's run shows the dominant pattern is:

```
err = "docker_run_failed: permission denied while trying to connect
       to the docker API at unix:///var/run/docker.sock"
n   = (count of all current FAILED_SANDBOX_SETUP rows)
```

That's a single, named issue — fix once (docker socket permissions
on the cifix worker container), unblock the whole class.

## What this does NOT do

1. **Doesn't fix the sandbox failures.** Observability tells you what
   to fix; the fix is a separate operator action. Today's diagnostic
   points at docker-socket permissions on the cifix worker.
2. **Doesn't catch all SRE failure shapes.** Only the canonical
   `sandbox_provisioning_failed` path produces a diagnostic. The
   `sre_blocked` path (agentic gap-fill couldn't resolve) does NOT
   currently classify as FAILED_SANDBOX_SETUP and so does NOT carry
   a diagnostic. If we start seeing sre_blocked in evidence, we'll
   extend the detector.
3. **Doesn't capture stderr from the underlying docker invocation.**
   The `error_message` comes from `task.error`, which is the
   `provisioned.error` string set by `provision_on_the_fly`. That's
   usually enough to identify the root cause; if it isn't, the SRE
   provisioner code would need to capture more stderr.

## Reconciler interaction

The reconciler ([phalanx/maintenance/ledger_reconciler.py](../../phalanx/maintenance/ledger_reconciler.py))
also uses the diagnostic path: when it heals a stale row whose run
ended up FAILED_SANDBOX_SETUP, the healed row picks up the same
structured diagnostic and synthesized root_cause. No special-casing
required — the reconciler reuses the same `_resolve_failure_class` +
synthesizer helpers as the CLI.

## Tests

[tests/unit/shadow/test_sandbox_observability.py](../../tests/unit/shadow/test_sandbox_observability.py) — 14 tests covering:

- Detector returns structured diagnostic on the canonical shape
- Detector returns None on healthy / completed / non-provisioning SRE failures
- error_message truncated to 500 chars
- Synthesizer produces operator-actionable one-liner
- Synthesizer truncated to 600 chars
- Provenance carries the diagnostic
- Schema version is 2
- v2 is a strict superset of v1

## Migration risk

Zero schema changes. Provenance is a JSONB column; adding a key to it
is a no-op at the DB level. Pre-P1-6 rows have v1 provenance without
`sre_setup_diagnostic`; readers handle the missing key gracefully
(return None).

## Rollback plan

1. Revert the P1-6 commit (one git revert).
2. Pre-P1-6 rows had `phalanx_root_cause=null` on FAILED_SANDBOX_SETUP;
   reverting just stops new rows from carrying the synthesized text.
   Already-recorded P1-6 rows keep their synthesized root_cause — that's
   honest evidence and doesn't need un-doing.
3. The schema version field in JSONB stays at 2 on already-written
   rows; that's a forward-only marker, fine to leave.

## Operational limitation that the diagnostic surfaced

The current `error_message` on every recent run is:

```
docker_run_failed: permission denied while trying to connect to the
docker API at unix:///var/run/docker.sock
```

This points at a real environmental issue in the cifix worker
container: it mounts `/var/run/docker.sock` but the user inside
the container lacks permission to use it. **That's a separate fix
item** (likely needs the worker to run as root or add the user to
the docker group), out of P1-6 scope. P1-6 just makes the failure
**visible** in the ledger; fixing it is the next task.
