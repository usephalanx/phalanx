# Beta Readiness Stabilization Plan

**Date:** 2026-05-11
**Source of truth:** [docs/phase-2c-week1-operational-report-2026-05-11.md](docs/phase-2c-week1-operational-report-2026-05-11.md)
**Scope:** operational trust + infrastructure correctness only. No new AI capabilities, no new agents, no architecture work.
**Pause status:** Week 2 dispatches paused until P0 items land.

---

## A. Prioritized stabilization backlog

Priority key: **P0** = blocks beta and blocks Week 2 resumption · **P1** = blocks beta but Week 2 can resume without it · **P2** = blocks beta only at scale.

Effort key: **S** = ≤ 1 day · **M** = 2–3 days · **L** = 4–7 days.

### P0 — must land before any further dispatches

| # | Item                                               | Effort | Risk if unfixed                                                                                       |
| - | -------------------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------- |
| 1 | Automated postgres dumps to off-host location      | S      | Next data-loss event erases all proof. Already happened once on 2026-05-11.                            |
| 2 | Per-dispatch ledger auto-export to `ledger.jsonl`  | S      | Even with backups, a single dispatch's evidence is at risk between snapshots. Source of W1.11 loss.   |
| 3 | Bootstrap-safety check before `docker compose up`  | S      | Mounting a fresh volume on top of a non-fresh compose project silently wipes data. Exactly what bit us.|
| 4 | Fresh-DB `alembic upgrade head` CI step            | S      | Latent migration bugs (20260321_0001 was latent 7 weeks). Beta = N new envs hitting these.            |
| 5 | W1.9 ledger/task divergence investigation (§C)     | M      | Ledger numbers are not auditable. Every claim we make from the ledger is suspect until resolved.      |

### P1 — must land before beta, can run in parallel with Week 2

| #  | Item                                                              | Effort | Risk if unfixed                                                              |
| -- | ----------------------------------------------------------------- | ------ | ---------------------------------------------------------------------------- |
| 6  | Structured FAILED_SANDBOX_SETUP diagnostics in ledger             | M      | 5/13 Week-1 entries had no actionable diagnosis. Beta users will see opaque ❌. |
| 7  | W1.10-style "SAFE_ESCALATE with empty diagnosis" post-condition   | S      | Conf=null + empty root_cause is operationally identical to silent FAILED.    |
| 8  | Cifix worker queue defined in dev compose (not sidecar)           | S      | Current dev compose requires manual `docker run`; fragile, undocumented.     |
| 9  | Documented restore drill (executed once end-to-end, screenshotted) | S      | Backups you've never restored are not backups.                               |
| 10 | Volume-binding pre-flight in dispatch CLI                          | S      | Catches the "wrong DB, dispatching anyway" failure mode.                     |

### P2 — must land before scaling beyond 5 maintainers

| #  | Item                                                  | Effort | Risk if unfixed                                                                          |
| -- | ----------------------------------------------------- | ------ | ---------------------------------------------------------------------------------------- |
| 11 | Status endpoint exposing queue depth + worker liveness | M      | Beta users can't self-diagnose "is Phalanx healthy". Every issue routes to you.          |
| 12 | Per-repo CIIntegration registration without DB seed   | M      | Today's seed flow requires me to insert rows from `$GH_TOKEN`. Not a beta-shippable UX.   |
| 13 | Sandbox-bootstrap smoke test on every commit          | M      | Prevents the 2/2 FAILED_SANDBOX_SETUP from a fresh stack class of regression.            |
| 14 | Healthcheck on `forge-cifix-worker`                    | S      | Today's container ran "unhealthy" by docker's measure; no actual liveness signal.        |
| 15 | Cost-cap pre-flight (refuse run if estimate > $5)     | S      | $25 cap is reactive. Pre-flight is preventive; Week 1 max was $0.80 so headroom is fine. |

---

## B. Exact implementation plan

### B1. Automated postgres backups (P0-1, effort: S)

**What:** Run `pg_dump` against `forge-postgres` every 6 hours, write the dump to `/Users/raj/forge/backups/postgres/`, retain 14 dumps (~3.5 days), upload the latest to an off-host bucket once daily.

**Files to add:**
- `scripts/backup_postgres.sh` — single-shot script: `docker exec forge-postgres pg_dump -U forge -F c forge > backups/postgres/forge-$(date -u +%Y%m%dT%H%M%SZ).dump` + retention prune to last 14 files.
- `scripts/backup_postgres_offhost.sh` — wraps a configured S3/R2/Backblaze put. Bucket name from `$PHALANX_BACKUP_BUCKET`; no hard-coded provider.
- `Makefile` targets: `backup` (one-shot), `backup-restore <dump>` (interactive restore).

**Schedule mechanism:**
- LaunchAgent at `~/Library/LaunchAgents/com.phalanx.backup.plist` for the user-host case (this is a Mac dev box).
- Cron equivalent (`scripts/backup_postgres.crontab`) included for non-Mac.

**Verification:** Restore a dump into a temporary postgres container, run `SELECT COUNT(*) FROM shadow_ledger;` against it, compare to live count. Wrap as `make backup-verify`.

**Acceptance criteria:**
- `make backup` produces a `.dump` file ≥ 10KB.
- `make backup-restore` brings up `forge-postgres-restore-test` container and reports row counts.
- The LaunchAgent runs at the configured cadence (verified via `launchctl list | grep phalanx`).
- One off-host upload has succeeded.

### B2. Per-dispatch ledger auto-export (P0-2, effort: S)

**What:** After every `update_with_results` call, append a JSON line of the row to `/Users/raj/forge/ledger.jsonl`. File-based, append-only, fsync after each write. No coordination needed because shadow dispatch is the only writer.

**Files to modify:**
- [phalanx/shadow/ledger.py:96-137](phalanx/shadow/ledger.py#L96-L137) — at end of `update_with_results`, after `await session.refresh(row)`, call `_append_jsonl(row)`.
- New helper `_append_jsonl(row)` in same file: opens `LEDGER_JSONL_PATH` in `a` mode, writes `json.dumps(to_dict(row)) + "\n"`, `fsync`, close. Path from `settings.ledger_jsonl_path` with default `/app/ledger.jsonl` (mounted to `/Users/raj/forge/ledger.jsonl`).
- [phalanx/config/settings.py](phalanx/config/settings.py) — add `ledger_jsonl_path: str = "/app/ledger.jsonl"`.

**Why JSONL not JSON:** append is O(1), no read-modify-write race, survives partial writes (one corrupt line ≠ corrupt file), trivial to grep/diff/audit.

**Verification:** Dispatch a run, observe a new line in `ledger.jsonl` within the same transaction's commit boundary. The line must match `phalanx shadow show <ledger_id>` byte-for-byte (excluding ordering).

**Acceptance criteria:**
- 100% of `update_with_results` calls produce exactly one new line.
- `wc -l ledger.jsonl` increases monotonically across dispatches.
- A regression test in `tests/unit/shadow/test_ledger_jsonl_export.py` asserts both the append and the byte-equality.

### B3. Bootstrap-safety check before `docker compose up` (P0-3, effort: S)

**What:** A pre-flight script that refuses to start the stack if the docker compose project name has shifted in a way that would silently mount a fresh volume.

**Files to add:**
- `scripts/preflight_check.sh` — invoked by `make up`. Runs three checks:
  1. Compose project name resolves to `phalanx-dev` (matches docker-compose.yml line 7).
  2. `docker volume inspect phalanx-dev_forge-postgres-data` exists; if it does, its `CreatedAt` is older than 1 day OR is matched by a successful backup; if not, refuse with an explicit warning that says "this will mount a fresh empty volume, data loss likely, set PHALANX_ALLOW_FRESH_BOOT=1 to confirm".
  3. `.env` file exists. (We hit this today — `.env.example` was the only env file present.)

**Files to modify:**
- `Makefile` — `up:` prepends `./scripts/preflight_check.sh && ` to the docker compose up line.

**Acceptance criteria:**
- A clean checkout of the repo on a fresh dev machine triggers the "fresh volume, data loss likely" refusal.
- Setting `PHALANX_ALLOW_FRESH_BOOT=1` allows the boot.
- A normal restart on the same machine passes silently.

### B4. Fresh-DB migration CI job (P0-4, effort: S)

**What:** A GitHub Actions job that, on every PR touching `alembic/` or `phalanx/db/models.py`, brings up a fresh postgres container, runs `alembic upgrade head`, asserts exit 0, then runs `alembic downgrade base && alembic upgrade head` to catch one-way migrations.

**Files to add:**
- `.github/workflows/migration-bootstrap.yml`:
  - Trigger: `pull_request` paths-filter on `alembic/**`, `phalanx/db/models.py`.
  - Job: `services.postgres` (pgvector/pgvector:pg16), `run: alembic -c alembic/alembic.ini upgrade head && alembic -c alembic/alembic.ini downgrade base && alembic -c alembic/alembic.ini upgrade head`.

**Acceptance criteria:**
- Reverting [alembic/versions/20260321_0001_add_dag_columns.py](alembic/versions/20260321_0001_add_dag_columns.py) to the broken VARCHAR version causes the CI job to fail.
- Current head passes both upgrade-from-zero and round-trip.

### B5. Sandbox setup observability (P1-6, effort: M)

**What:** Make FAILED_SANDBOX_SETUP entries carry enough information that the ledger row alone tells you what to fix.

**Today's gap:** The detector ([phalanx/shadow/runner.py:304](phalanx/shadow/runner.py#L304)) sets `failure_class=FAILED_SANDBOX_SETUP` but the ledger's `phalanx_root_cause` stays `null` because the SRE setup task fails before TL writes any output. The runner has the task evidence but doesn't surface it.

**Files to modify:**
- [phalanx/shadow/runner.py:304-…](phalanx/shadow/runner.py#L304) `_detect_sre_setup_failure` — extend the function to **also** return a structured reason payload: `{"failed_step": str, "command": str, "stderr_tail": str}`. Already has the SRE task in hand; just needs to read `task.output["last_failed_step"]`/`task.error` and trim.
- [phalanx/shadow/runner.py:533-552](phalanx/shadow/runner.py#L533-L552) — when `failure_class == "FAILED_SANDBOX_SETUP"`, pass a synthesized `root_cause` string built from the structured reason: `"sandbox bootstrap failed at step '<failed_step>': <stderr_tail>"`. This goes into `phalanx_root_cause` so the ledger row shows it.
- [phalanx/agents/cifix_sre.py](phalanx/agents/cifix_sre.py) — ensure the SRE setup task writes `last_failed_step`, `failed_command`, `stderr_tail` to its `Task.output` on failure. (May already exist; verify before writing.)

**Why a single `phalanx_root_cause` string and not new columns:** keep ledger schema stable. Structured payload lives in `Task.output`; ledger gets the human-readable summary.

**Acceptance criteria:**
- Re-dispatching W1.12 and W1.13 produces ledger rows where `phalanx_root_cause` is non-null and points at the specific apt/pip/git step that failed.
- A regression test in `tests/unit/shadow/test_failed_sandbox_diagnostics.py` injects a synthetic SRE failure and asserts both the failure_class and the root_cause are populated.

### B6. Cifix worker in dev compose (P1-8, effort: S)

**What:** Move the ad-hoc `docker run --name forge-cifix-worker` into `docker-compose.yml` as a first-class service. Mirrors the prod compose definition.

**Files to modify:**
- [docker-compose.yml](docker-compose.yml) — add `forge-cifix-worker` service after `phalanx-worker`, with the queue list `cifix_commander,cifix_techlead,cifix_engineer,cifix_sre,cifix_sre_verify,cifix_challenger`, concurrency=2, and a healthcheck (`celery -A phalanx.queue.celery_app inspect ping -d worker-cifix@$$HOSTNAME`).

**Acceptance criteria:**
- A clean `make up` brings the cifix worker up automatically.
- `docker compose ps` shows it as `healthy`.
- A shadow dispatch succeeds end-to-end against the compose-managed worker (no `docker run` sidecar).

---

## C. W1.9 ledger/task divergence — investigation plan

### Symptoms

`shadow_ledger` row for psf/black wf=25407733985 (Week 1 W1.9) recorded:
- `phalanx_verdict=FAILED`
- `phalanx_confidence=0.82`
- `phalanx_root_cause` references CHANGES.md needing an entry

The corresponding `tasks` row(s) for `agent_role=cifix_techlead` were reported (in the conversation that drove the report) as carrying:
- `output.confidence=0.0`
- `output.review_decision=ESCALATE`

A ledger derived from the TL task output should never disagree with that output.

### Exact codepaths involved

1. **TL writes output** — [phalanx/agents/cifix_techlead.py:921-929](phalanx/agents/cifix_techlead.py#L921-L929) returns `AgentResult(output={**fix_spec, ...})`. On `success=False`/`plan_validation_failed` it instead returns the structure at [lines 903-912](phalanx/agents/cifix_techlead.py#L903-L912), which still spreads `**fix_spec` (so confidence + root_cause survive even on validation failure).

2. **Task.output gets persisted** — wherever `AgentResult.output` is written into `Task.output` (likely `phalanx/runtime/agent_runner.py` or similar — investigation step T1 below).

3. **Ledger reads TL output** — [phalanx/shadow/runner.py:274-301](phalanx/shadow/runner.py#L274-L301) `_read_terminal_evidence` selects all Task rows for the run, then keeps the **LAST occurrence per role** ([line 286](phalanx/shadow/runner.py#L286): `by_role[t.agent_role] = t` — comment says "iter-N wins").

4. **Ledger writes** — [phalanx/shadow/runner.py:525-528](phalanx/shadow/runner.py#L525-L528):
   ```python
   confidence = float(tl.get("confidence") or 0.0) if isinstance(tl, dict) and tl.get("confidence") is not None else None
   root_cause = tl.get("root_cause") if isinstance(tl, dict) else None
   ```

### Hypotheses (ranked by likelihood)

**H1 — Multiple TL iterations + selection bug (most likely).** The run had ≥2 TL tasks. `by_role` keeps the LAST one by iteration order. If `sequence_num` of iter-2 is lower than iter-1 due to a DAG insertion bug, the "last" we keep is actually the earlier one (the 0.82). The displayed task output (0.0/ESCALATE) is iter-2, but the ledger reads iter-1.

  - **Disprover:** for the W1.9 run, list all `cifix_techlead` Task rows ordered by `sequence_num ASC` and by `created_at ASC`. If both orderings put 0.82 last, H1 is wrong.

**H2 — TL task `output` was mutated after the ledger write.** The runner reads TL output at ledger-write time. If a later process (re-run, repair task, post-mortem reconciler) overwrites `Task.output` to inject `review_decision=ESCALATE`, the ledger preserves the original 0.82 while the current task shows 0.0.

  - **Disprover:** there is no such reconciler in the codebase. Confirm by `git grep -n "Task.output =\\|update.*tasks.*output"`. If no write-after-terminal site exists, H2 is wrong.

**H3 — Engineer task masquerading as TL output.** A code path that copies engineer output into the TL Task row, or vice versa. Would only happen on a refactor bug.

  - **Disprover:** for the W1.9 run, fetch the engineer Task row and check whether its output contains `confidence=0.82` and the CHANGES.md root_cause. If the engineer output is shaped differently from the ledger's recorded fields, H3 is wrong.

**H4 — `Task.output` is JSON-stringified inconsistently.** If `tl.output` is sometimes a dict and sometimes a JSON string, `tl.get("confidence")` returns `None` for strings, but a different code path (the displayed task output in pgAdmin) might be deserializing it differently from the runner.

  - **Disprover:** check the column type on `tasks.output` in [phalanx/db/models.py](phalanx/db/models.py). If it's `JSONB` (not `Text`), H4 is wrong.

### Instrumentation to add

Add these regardless of which hypothesis pans out — they're cheap and they catch the next divergence automatically.

1. **At ledger-write time, log the full TL output payload alongside the derived ledger row.** Add to [phalanx/shadow/runner.py:533](phalanx/shadow/runner.py#L533) just before `update_with_results`:
   ```python
   log.info(
       "shadow.ledger_write.preimage",
       run_id=run_id,
       tl_task_count=sum(1 for t in tasks if t.agent_role == "cifix_techlead"),
       tl_output_chosen_confidence=confidence,
       tl_output_chosen_review_decision=tl.get("review_decision"),
       tl_output_chosen_root_cause_head=(root_cause or "")[:120],
   )
   ```

2. **A new audit-trail column** `phalanx_provenance JSONB` on `shadow_ledger` (additive migration) that captures, at ledger-write time, the source task_id + the task's `created_at` + `sequence_num`. So every ledger row points at exactly which task row it derived from. Future divergences become provably attributable.

3. **A consistency assertion in tests.** A new regression test in `tests/unit/shadow/test_ledger_task_consistency.py` that runs a shadow dispatch end-to-end against a fixture and asserts:
   ```python
   tl_task = await session.get(Task, ledger.phalanx_provenance["tl_task_id"])
   assert ledger.phalanx_confidence == tl_task.output.get("confidence")
   assert ledger.phalanx_root_cause == tl_task.output.get("root_cause")
   ```

### Proof criteria for resolution

W1.9 is considered resolved when **all** of the following hold:

- The root cause hypothesis has been identified by reading the run's actual Task rows (requires the run's data; today only run_id 25407733985 is known — its DB rows are gone with the rest, so this specific run cannot be re-investigated. The plan is to **catch the next occurrence** with the instrumentation above and resolve on a live example).
- A failing regression test exists that reproduces the divergence pre-fix.
- The fix passes the regression test plus the consistency assertion (instrumentation #3).
- The audit-trail column is in place for forward auditability.
- Re-dispatching 5 shadow runs (mix of SHIPPED / SAFE_ESCALATE / FAILED) shows zero `(ledger, task)` divergence.

---

## D. Definition of "operationally trustworthy"

Concrete, measurable, none of these can be self-graded.

### Required to be true before beta launch

1. **Ledger durability**
   - ≥1 successful end-to-end restore drill executed and logged.
   - Automated dumps running on schedule.
   - `ledger.jsonl` append-after-write in place.
   - Zero data-loss events in the trailing 14 days.

2. **Bootstrap correctness**
   - `make up` on a clean checkout works without manual intervention.
   - `alembic upgrade head` from an empty postgres succeeds in CI on every PR.
   - Bootstrap pre-flight check refuses to silently mount a fresh volume.

3. **Ledger ↔ task consistency**
   - W1.9 root cause identified (or the instrumentation has been in place long enough to prove the divergence does not recur — minimum 20 dispatches).
   - `phalanx_provenance` column populated on every new row.
   - Regression test for consistency exists and is green.

4. **Sandbox diagnostics**
   - Every FAILED_SANDBOX_SETUP entry in the trailing 14 days has a non-null `phalanx_root_cause`.
   - Zero ledger rows with `verdict in (SAFE_ESCALATE, FAILED)` and `root_cause IS NULL`.

5. **Observability**
   - Heartbeat events visible per-role for every running task.
   - Worker liveness queryable via a simple HTTP endpoint.
   - The cifix worker is in compose (not a sidecar).

### 7-day green-streak metrics

Metrics that must stay green for **7 consecutive days** before flipping the beta switch:

| Metric                                                                | Target               |
| --------------------------------------------------------------------- | -------------------- |
| Dispatches completed with terminal verdict                            | 100%                 |
| Dispatches with `verdict=SHIPPED` or `verdict=SAFE_ESCALATE` and `root_cause IS NOT NULL` | 100%                 |
| FAILED_SANDBOX_SETUP entries with `root_cause IS NOT NULL`            | 100%                 |
| Ledger rows where `(ledger.confidence, task.output.confidence)` agree | 100%                 |
| Stuck task detector firings                                            | 0 unhandled          |
| Postgres dump cadence                                                  | every 6h, no misses  |
| `alembic upgrade head` CI passes                                       | 100% on main         |
| Side-effect audit (any push, commit, PR by Phalanx)                    | 0                    |
| Cost cap breaches                                                      | 0                    |

7 days of green is not 7 days of nothing-happened — it requires at least 10 dispatches in that window, otherwise the streak doesn't count.

---

## E. Minimal beta launch checklist

The smallest version we can hand to 3–5 friendly maintainers without lying to them.

### Pre-flight (one-time)

- [ ] P0-1 backups: automated dumps running, ≥1 restore drill completed, dump file off-host.
- [ ] P0-2 ledger.jsonl: auto-append in place, regression test green.
- [ ] P0-3 bootstrap pre-flight: refuses fresh-volume mount without explicit env flag.
- [ ] P0-4 fresh-DB CI: GitHub Actions job green on a PR that intentionally breaks it (proves the check works).
- [ ] P0-5 W1.9 investigation: either resolved on a reproduced run, or instrumentation has been in place for 20+ dispatches with zero divergence observed.
- [ ] P1-6 sandbox diagnostics: every FAILED_SANDBOX_SETUP carries a structured root_cause.
- [ ] P1-7 empty-diagnosis post-condition: SAFE_ESCALATE with null root_cause is impossible (write fails or writes a synthesized reason).
- [ ] P1-8 cifix worker in dev compose.

### 7 days before opt-in

- [ ] Trailing 7-day metrics (table in §D) all green.
- [ ] ≥10 dispatches during the streak window, mix of at least 3 archetypes.
- [ ] At least 5 SHIPPED_PROPOSED rows total (across any time window), each independently inspected and confirmed correct.

### Maintainer onboarding (per maintainer)

- [ ] CIIntegration row seeded via the registration flow (not direct DB insert). P2-12 closes this; if not done, document the manual workaround clearly.
- [ ] Maintainer agrees in writing that Phalanx will only observe — no commits, no PRs, no comments.
- [ ] Maintainer is given the ledger URL and shown one SHIPPED + one SAFE_ESCALATE example for their repo.
- [ ] An off-switch is documented and works: a single curl to disable Phalanx for that repo.

### Continuous operation

- [ ] Daily: confirm dump landed.
- [ ] Daily: confirm ledger.jsonl has grown by expected amount.
- [ ] Weekly: archetype coverage report, side-effect audit, cost report.
- [ ] Weekly: one randomly-sampled SAFE_ESCALATE re-read by you and rated for diagnosis quality.

### Off-ramp

A pre-written "we're pausing the beta" message and the procedure to disable every CIIntegration in one operation. Must be written before launch, not improvised during an incident.

---

## What this plan is NOT doing

Explicitly out of scope, recorded so we don't drift:

- No new agents.
- No model upgrades or prompt tuning.
- No multi-iteration TL changes.
- No new failure-class detectors beyond surfacing what FAILED_SANDBOX_SETUP already has.
- No write-access experiments. SHIPPED_PROPOSED stays observation-only.
- No GitHub App work (that's a separate milestone after invite-only proves out).

If any of those start feeling tempting during stabilization work, that's the signal that we're drifting back into capability work. Stop, finish stabilization, then revisit.
