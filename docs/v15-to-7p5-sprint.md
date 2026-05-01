# CI Fixer v3 — sprint to 7.5/10 (Phase 0 spec)

**Status**: Phase 0 spec lock (2026-05-01). Implementation Mon-Sat per the plan in chat. Each Mon-Sat phase has CONTRACTS below — implementation is "code to spec", not "discover requirements as we go".

**Definition of Done — 7.5/10** (binary checklist):
- [ ] Phase 1: validator catches ≥80% of seeded dishonest fix_specs
- [ ] Phase 2: reaper kills synthetic stuck-run within 6min; cost cap aborts at threshold
- [ ] Phase 3: humanize `a47a89e` revert → v3 re-derives equivalent fix; real CI green on v3's commit
- [ ] Phase 4: dashboard URL serves non-zero data on all 5 views
- [ ] Phase 5: stress harness N=20 → ≥90% SHIPPED, p95 ≤ 15min, ≤1 reaper hit
- [ ] Phase 6: install link works end-to-end on a fresh 3rd test repo

If any item RED at Saturday close, ship at actual score. No fudging.

---

## Phase 1 — Self-critique validator (Mon)

### Goal
TL self_critique booleans become tool-validated truths instead of LLM declarations.

### Contract — new tool `validate_self_critique`

```python
ToolSchema(
    name="validate_self_critique",
    description="REQUIRED before emit_fix_spec. Validates that the candidate self_critique booleans are actually true given the diagnosis. Returns the AUTHORITATIVE booleans, replacing the LLM's draft.",
    input_schema={
        "type": "object",
        "properties": {
            "draft_root_cause": {"type": "string"},
            "draft_affected_files": {"type": "array", "items": {"type": "string"}},
            "draft_verify_command": {"type": "string"},
            "ci_log_text": {"type": "string", "description": "verbatim text from fetch_ci_log earlier in the turn"},
        },
        "required": ["draft_root_cause", "draft_affected_files", "draft_verify_command", "ci_log_text"],
    },
)
```

### Validation logic (deterministic, in tool handler)

1. **`ci_log_addresses_root_cause`** = true iff:
   - extract distinctive ≥4-char tokens from `draft_root_cause` (drop common words)
   - count of those tokens that appear in `ci_log_text` ≥ 1 AND ≥ 30% of extracted tokens

2. **`affected_files_exist_in_repo`** = true iff:
   - every path in `draft_affected_files` resolves under workspace_path (no traversal) AND `is_file()` returns True

3. **`verify_command_will_distinguish_success`** = true iff:
   - first shell token of `draft_verify_command` matches `^[A-Za-z0-9._\-]+$`
   - sandbox `command -v <token>` returns 0

### Wiring

- TL system prompt: replace existing self_critique guidance with: "MUST call validate_self_critique before emit_fix_spec. Use returned booleans verbatim — do NOT overwrite."
- Tool dispatch: handler runs the 3 checks; returns `{validated: {c1,c2,c3}, mismatches: [...]}`. TL must consume this and place returned values into the fix_spec's self_critique field.
- Commander gate (in `cifix_commander.execute` after TL task completes):
  - parse fix_spec
  - if any of `self_critique.{c1,c2,c3}` is false → mark TL task FAILED with reason `self_critique_mismatch:<which>`
  - dispatch ONE retry (re-run TL); if second TL also returns false → ESCALATED with `escalation_reason=self_critique_repeated_failure`

### Out-of-scope
- Engineer self-critique (deferred to v1.7)
- SRE setup self-critique (deferred to v1.7)

### Acceptance (tier-1 corpus)
Build 10-fix_spec corpus in `tests/integration/v3_harness/fixtures/self_critique_corpus.py`:
- 5 honest: root_cause matches log, files exist, verify_command resolvable
- 5 dishonest: 1 wrong root_cause keyword, 2 missing files, 2 unresolvable verify_command first-token

Validator must:
- pass all 5 honest
- catch all 5 dishonest with the right `mismatches[]` reason

Plus internal canary: testbed lint + test_fail still SHIP unchanged.

### Files to touch
- `phalanx/agents/cifix_techlead.py` (prompt + new tool)
- `phalanx/agents/cifix_commander.py` (gate)
- `phalanx/ci_fixer_v3/sre_setup/tools.py` (NO — different tool registry)
- new: `phalanx/agents/_tl_self_critique.py` (validator handler)
- tests: `tests/integration/v3_harness/test_tl_self_critique.py`

---

## Phase 2 — Reaper + per-run cost cap (Tue)

### Goal A — Reaper
Stuck v3 runs auto-cancelled within 6 min. Replaces existing `check_blocked_runs` stub at `phalanx/maintenance/tasks.py:14`.

### Contract A

```python
async def _check_blocked_runs_impl() -> dict[str, int]:
    """Find Run rows in EXECUTING/VERIFYING with no Task progress >30min;
    mark FAILED. Returns count killed."""
    cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_RUN_THRESHOLD_MINUTES)
    killed = 0
    async with get_db() as session:
        rows = await session.execute(
            select(Run).where(
                Run.status.in_(["EXECUTING", "VERIFYING"]),
                Run.updated_at < cutoff,
            )
        )
        for run in rows.scalars():
            await session.execute(
                update(Run).where(Run.id == run.id).values(
                    status="FAILED",
                    error_message=f"reaper: stuck > {STUCK_RUN_THRESHOLD_MINUTES}min (state={run.status})",
                )
            )
            await session.execute(
                update(Task).where(
                    Task.run_id == run.id,
                    Task.status.in_(["IN_PROGRESS", "PENDING"]),
                ).values(status="FAILED", error="reaper: parent run terminated")
            )
            killed += 1
        await session.commit()
    return {"killed": killed}
```

`STUCK_RUN_THRESHOLD_MINUTES = 30`. Beat schedule: 5 min (already configured).

### Goal B — Per-run cost cap
`MAX_RUN_COST_USD = 1.0`. Aggregate `tasks.tokens_used` per run; abort dispatch if estimate exceeds.

### Contract B

```python
# in phalanx/agents/cifix_commander.py
COST_PER_TOKEN_USD = 20e-6  # blended GPT-5.4 + Sonnet, conservative
MAX_RUN_COST_USD = 1.0

async def _check_cost_cap(self, session) -> bool:
    """Returns True iff dispatch should ABORT due to cost cap."""
    result = await session.execute(
        select(func.coalesce(func.sum(Task.tokens_used), 0))
        .where(Task.run_id == self.run_id)
    )
    total_tokens = result.scalar() or 0
    estimate = total_tokens * COST_PER_TOKEN_USD
    if estimate > MAX_RUN_COST_USD:
        log.warning("v3.commander.cost_cap_exceeded", run_id=self.run_id,
                    tokens=total_tokens, estimate_usd=round(estimate, 3))
        await session.execute(
            update(Run).where(Run.id == self.run_id).values(
                status="FAILED",
                error_message=f"cost_cap: ${estimate:.2f} > ${MAX_RUN_COST_USD}",
            )
        )
        await session.commit()
        return True
    return False
```

Call from `cifix_commander.execute` BEFORE each agent dispatch (sre_setup, TL, engineer, sre_verify, iter-2 TL, iter-2 engineer, iter-2 verify).

### Acceptance
- Tier-2 test: stale Run row + reaper invocation → row.status=FAILED, all child tasks FAILED, all within one beat cycle (6 min real, 1 sec test via direct call)
- Tier-2 test: tasks summing to 60_000 tokens; commander dispatches one more agent; abort fires before agent run; tokens_used > 50_000 → estimate > $1.0

### Files to touch
- `phalanx/maintenance/tasks.py`
- `phalanx/agents/cifix_commander.py`
- tests: `tests/integration/v3_harness_t2/test_reaper.py`, `tests/integration/v3_harness_t2/test_cost_cap.py`

---

## Phase 3 — Wild-bug proof (Wed)

### Goal
v3 re-derives humanize commit `a47a89e` (tz-aware datetime fix) given a synthetic revert PR. **First wild evidence.**

### Protocol
1. `cd /tmp/humanize-regress && git checkout main && git pull`
2. `git checkout -B path1/tz-fix-revert main`
3. `git revert --no-commit a47a89e -- src/humanize/time.py` — revert ONLY the src changes; keep the test file (which is the failure detector)
4. `git commit -m "path1: revert tz-fix to test v3 re-derivation"`
5. `git push -u origin path1/tz-fix-revert`
6. Open PR via `gh api` with `changelog: skip` label
7. Wait for CI to fail (the kept-tests will fail without the src fix)
8. Watch v3 dispatch via prod DB query
9. Wait for v3 terminal state (cap 30min)

### Acceptance (manual review, binary)
- v3 final_status = SHIPPED
- v3's commit modifies `src/humanize/time.py`
- Real GitHub CI re-runs on v3's commit and goes green
- Manual diff review: v3's change addresses `tz-aware datetime in naturalday/naturaldate`. Acceptable variants:
  - exact same approach as original (compute today in tz)
  - functionally equivalent (e.g., normalize value to UTC before comparison)
- NOT acceptable: changes that don't address timezone handling

### Failure path
If v3 ships wrong fix or escalates, capture full task chain + final commit, file as bug, move on. Don't iterate-to-fix in this phase.

### Files
- `scripts/v3_path1_humanize_tz.sh` (new) — implements steps 1-9 above
- New memory file post-run: `project_v3_path1_humanize_tz_result.md`

---

## Phase 4 — Observability surface (Thu)

### Goal
5 SQL views + dashboard URL + Slack alert.

### Contract — Migration `20260502_0001_v3_observability_views.py`

```sql
-- v_v3_terminal_state_24h: count by status, last 24h
CREATE OR REPLACE VIEW v_v3_terminal_state_24h AS
SELECT r.status, COUNT(*) AS n
FROM runs r
JOIN work_orders w ON w.id = r.work_order_id
WHERE w.work_order_type = 'ci_fix' AND r.created_at > NOW() - INTERVAL '24 hours'
GROUP BY r.status;

-- v_v3_fix_rate_by_category: SHIPPED/FAILED/ESCALATED per failure_category
CREATE OR REPLACE VIEW v_v3_fix_rate_by_category AS
SELECT cfr.failure_category, r.status, COUNT(*) AS n
FROM ci_fix_runs cfr
JOIN work_orders w ON w.title LIKE 'Fix CI: ' || cfr.repo_full_name || '#%'
JOIN runs r ON r.work_order_id = w.id
WHERE r.created_at > NOW() - INTERVAL '7 days'
GROUP BY cfr.failure_category, r.status;

-- v_v3_cost_per_run: estimated cost per run
CREATE OR REPLACE VIEW v_v3_cost_per_run AS
SELECT
  r.id AS run_id,
  COALESCE(SUM(t.tokens_used), 0) AS total_tokens,
  COALESCE(SUM(t.tokens_used), 0) * 20e-6 AS estimated_usd,
  r.status,
  r.created_at
FROM runs r
JOIN work_orders w ON w.id = r.work_order_id
LEFT JOIN tasks t ON t.run_id = r.id
WHERE w.work_order_type = 'ci_fix'
  AND r.created_at > NOW() - INTERVAL '7 days'
GROUP BY r.id, r.status, r.created_at;

-- v_v3_false_positive_rate: SHIPPED runs where push commit's CI later went red
-- (best-effort: compares Engineer commit_sha to ci_fix_runs follow-up rows)
CREATE OR REPLACE VIEW v_v3_false_positive_rate AS
WITH shipped AS (
  SELECT r.id, t.output->>'commit_sha' AS fix_sha, w.title
  FROM runs r
  JOIN work_orders w ON w.id = r.work_order_id
  JOIN tasks t ON t.run_id = r.id AND t.agent_role = 'cifix_engineer'
  WHERE r.status = 'SHIPPED'
    AND w.work_order_type = 'ci_fix'
    AND r.created_at > NOW() - INTERVAL '7 days'
)
SELECT
  s.id AS run_id,
  s.fix_sha,
  EXISTS(
    SELECT 1 FROM ci_fix_runs cfr
    WHERE cfr.commit_sha LIKE s.fix_sha || '%'
      AND cfr.created_at > NOW() - INTERVAL '1 hour'
      AND cfr.status IN ('PENDING', 'FIXING')  -- a webhook fired AFTER our fix
  ) AS post_fix_ci_failed
FROM shipped s;

-- v_v3_reaper_kills: runs the reaper killed in last 7d
CREATE OR REPLACE VIEW v_v3_reaper_kills AS
SELECT id, error_message, created_at
FROM runs
WHERE error_message LIKE 'reaper:%'
  AND created_at > NOW() - INTERVAL '7 days';
```

### Dashboard

Single FastAPI endpoint `GET /dashboard/v3` returns HTML page rendering each view as a table. Auth: same as `/admin/*` (basic-auth from existing settings). No JS framework — server-rendered HTML.

### Slack alert
Beat task `phalanx.maintenance.tasks.v3_quality_alert` runs hourly:
- query v_v3_false_positive_rate; if `post_fix_ci_failed` true rate > 5% over last 24h, post Slack message
- query v_v3_reaper_kills; if count > 3 in last hour, post Slack message
- Slack webhook URL in `settings.slack_quality_alert_webhook`

### Acceptance
- Migration runs cleanly in prod
- Dashboard accessible from laptop (basic auth) with non-zero rows on all 5 views (after Phase 3 + a few canary runs)
- Slack alert test: manually POST to webhook with synthetic payload — message arrives

### Files
- new alembic: `alembic/versions/20260502_0001_v3_observability_views.py`
- new route: `phalanx/api/routes/v3_dashboard.py`
- update: `phalanx/maintenance/tasks.py` (add v3_quality_alert)
- update: `phalanx/queue/celery_app.py` (add hourly schedule)

---

## Phase 5 — Concurrency stress (Fri)

### Goal
Validate v3 handles 5 / 10 / 20 concurrent runs.

### Harness — `scripts/v3_stress_n_concurrent.sh N`

```bash
#!/usr/bin/env bash
# Fire N synthetic testbed PRs in quick succession; collect stats.
set -u
N="${1:-5}"
LOG_DIR="/tmp/v3_stress_$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"

for i in $(seq 1 "$N"); do
  bash scripts/v3_python_regression.sh lint > "$LOG_DIR/cell_$i.log" 2>&1 &
  # Stagger by 2s to avoid GitHub-side rate-limit thunder
  sleep 2
done
wait

# Collect stats
python3 scripts/v3_stress_summarize.py "$LOG_DIR"
```

`scripts/v3_stress_summarize.py` parses each log, extracts run_id + verdict + wall, prints CSV summary + p50/p95.

### Stages
- N=5: ≥95% SHIPPED, p95 ≤ 5min — must pass before N=10
- N=10: ≥95% SHIPPED, p95 ≤ 8min — must pass before N=20
- N=20: ≥90% SHIPPED, p95 ≤ 15min, reaper hits ≤ 1

### Acceptance
- Each stage passes its bar
- All transient failures explainable (stage logged with reason)
- Recommendation document on max safe concurrency for beta

### Failure path
If N=20 reveals architectural issue (docker daemon serialization, sandbox-pool exhaustion, etc.), document + ship with `max_concurrent=N` policy where N is the highest passing stage.

### Files
- new: `scripts/v3_stress_n_concurrent.sh`
- new: `scripts/v3_stress_summarize.py`
- new memory file: `project_v3_concurrency_findings.md`

---

## Phase 6 — GitHub App + beta-ready (Sat)

### Goal
Working GitHub App: install link, OAuth callback, installation tokens, multi-tenant credential resolution. Replaces hardcoded PAT lookups for repos installed via the App; PAT path stays for testbed/humanize compat.

### Contract — DB

```sql
CREATE TABLE github_app_installations (
    id BIGSERIAL PRIMARY KEY,
    installation_id BIGINT NOT NULL UNIQUE,
    account_login TEXT NOT NULL,                -- the org/user that installed
    repo_full_names TEXT[] NOT NULL DEFAULT '{}',
    suspended BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX gh_app_install_repos ON github_app_installations USING gin(repo_full_names);
```

### Contract — credential resolver

```python
# in a new file phalanx/ci_fixer/github_app_creds.py
async def resolve_github_token_for_repo(repo_full_name: str) -> str | None:
    """v1.6: tries installation-token first (App path); falls back to
    ci_integrations.github_token (PAT path) for legacy repos like testbed
    + humanize. Caches installation tokens for ~50 min (TTL is 1h)."""
    cached = _installation_token_cache.get(repo_full_name)
    if cached and cached.expires_at > datetime.now(UTC) + timedelta(minutes=10):
        return cached.token
    # ... query github_app_installations for repo; mint installation token
    # via JWT signed with App private key + GitHub's /app/installations/<id>/access_tokens
```

Replace `_resolve_github_token` callers in:
- `phalanx/agents/cifix_techlead.py`
- `phalanx/agents/cifix_engineer.py`
- `phalanx/agents/cifix_sre.py`
- `phalanx/api/routes/ci_webhooks.py`

with the new resolver. Existing PAT path stays as fallback.

### Contract — webhook + install routes

- Existing `POST /webhook/github` keeps working for legacy repos
- New: webhook signature verification accepts BOTH the legacy secret AND the App webhook secret
- New: `GET /github/app/install/callback` handles GitHub's redirect after install
- New: `POST /github/app/install/event` receives App-level events (installation, installation_repositories) — INSERT/UPDATE github_app_installations

### Required env vars
- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY` (PEM, multi-line — use base64 in env, decode at startup)
- `GITHUB_APP_WEBHOOK_SECRET`
- Existing `GITHUB_WEBHOOK_SECRET` stays for legacy

### Install link
`https://github.com/apps/<app-name>/installations/new`

### Acceptance
- Install on a fresh 3rd test repo (e.g., `usephalanx/canary-app-test`)
- Webhook fires for that repo
- v3 dispatches using installation token (logged: `auth_method=installation_token`)
- v3 commits a fix to that repo using the installation token
- testbed + humanize still work (PAT path regression)

### Out-of-scope this sprint
- Marketplace listing (3-week review)
- Billing / pricing tiers
- Public marketing site
- App icon design

### Files
- new alembic: `alembic/versions/20260503_0001_github_app_installations.py`
- new model: addition to `phalanx/db/models.py`
- new module: `phalanx/ci_fixer/github_app_creds.py`
- new route: `phalanx/api/routes/github_app.py`
- updates: 4 agent files for resolver swap
- new scripts: `scripts/setup_github_app.md` (operator-facing checklist)

---

## Risk register (cross-phase)

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| Phase 1 validators reject legitimate fix_specs (over-strict) | med | high | tier-1 corpus tests both directions; tunable thresholds (e.g., keyword-overlap %); tier-2 canary on testbed before deploy |
| Phase 3 v3 produces wrong fix on humanize tz revert | med | med | ACCEPTABLE OUTCOME — file as bug, capture data, ship at 7.0 instead of 7.5; do NOT iterate within Phase 3 |
| Phase 5 N=20 reveals architectural concurrency limit | low | high | ship with `max_concurrent=N_max_safe` policy; document; treat as known limitation for v1.6 |
| Phase 6 OAuth flow has edge cases (org install vs user install) | med | low | scope to user-install only for beta; org-install in v1.7 |
| Cross-phase: v3 prod has a quiet bug we don't surface until N+ runs | high | med | dashboards + Slack alerts (Phase 4) catch it within hours, not days |

## Cross-cutting Definition of NOT Done (i.e. don't ship)

- Self-critique false negative rate > 20% on tier-1 corpus
- Reaper has any false-positive (kills a healthy run)
- Cost cap fires on any internal-Python testbed canary (would mean policy is wrong)
- Path 1 surfaces a destructive fix attempt (tries to delete unrelated files, etc.)
- Stress at N=5 reveals stuck runs

Any of these = STOP, reassess scope, ship at lower number with documented gaps.

---

## How we measure 7.5 at end-of-sprint

```sql
-- Quality scorecard query (run end of Sat)
SELECT
  (SELECT COUNT(*) FROM v_v3_terminal_state_24h WHERE status = 'SHIPPED') AS shipped_24h,
  (SELECT COUNT(*) FROM v_v3_reaper_kills) AS reaper_kills_7d,
  (SELECT AVG(estimated_usd) FROM v_v3_cost_per_run WHERE status = 'SHIPPED') AS avg_shipped_cost,
  (SELECT COUNT(*) FILTER (WHERE post_fix_ci_failed) * 100.0
          / NULLIF(COUNT(*), 0) FROM v_v3_false_positive_rate) AS false_positive_pct;
```

Target end-of-sprint:
- shipped_24h ≥ 30 (across stress runs + canaries)
- reaper_kills_7d ≤ 5 (acceptable; some legitimately stuck runs)
- avg_shipped_cost ≤ $0.50
- false_positive_pct ≤ 5%

If these numbers are green, you ship to 3-5 invite-only maintainers Monday.
