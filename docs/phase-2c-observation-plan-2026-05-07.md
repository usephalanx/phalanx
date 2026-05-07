# Phalanx CI Fixer — Phase 2c observation plan (2026-05-07)

**Stack today**: v1.7.3-build-essential (commit `b2aefa1`)
**Ledger today**: 21 attempts / 14 unique workflows / 11 repos / 79%
SAFE_ESCALATE / 0 side effects / $3.33 spent
**Phase 2c target**: 100+ attempts at the same architectural fidelity

This is an **operational** doc, not a code-design doc. It defines how
we accumulate observation data without adding meaningful code, and
sets explicit thresholds for the next two trust gates: private beta,
then GitHub App write access.

---

## 1. Weekly execution plan

### Cadence

**12–15 shadow attempts / week × 8 weeks ⇒ 100–120 ledger rows.**

Each attempt averages $0.16 + 3.3 min wall-clock today. The week's
dispatch is ~$2.40 + ~50 min, well within any operational budget.

### Mechanics

Manual dispatch via the existing CLI. Operator-driven, not poller-
driven, until at least 50 entries.

```bash
python -m phalanx.shadow run \
    --repo owner/name \
    --workflow-run-id <id> \
    --poll-interval 15 --poll-timeout 1500
```

Why no auto-poller yet: at 12–15 runs/week it's 2–3 entries/day —
trivial to triage manually. A poller adds infrastructure (workflow-
run discovery, dedup, scheduling) that buys nothing operationally
until volume crosses ~30/day.

### Weekly rhythm

| Day | Action | Time |
|---|---|---|
| Mon | Survey fresh failures across watch-list (script below) | 5 min |
| Mon | Pick 5–7 entries; dispatch sequentially | 30–60 min |
| Tue–Wed | Dispatch 5–7 more; spread across day | 30–60 min |
| Thu | Run `phalanx-shadow metrics --by-workflow`; eyeball deltas | 10 min |
| Fri | Side-effect audit (script below) across all repos touched this week | 10 min |

Total operator time: ~2 hours/week.

### Survey script (one-shot, idempotent)

```bash
# scripts/phase2c_survey.sh
for repo in $(cat config/phase2c_watchlist.txt); do
  echo "=== $repo ==="
  curl -sH "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$repo/actions/runs?status=failure&per_page=5&created=>$(date -u -v-3d +%Y-%m-%d)" \
    | jq -r '.workflow_runs[] | "  \(.id) PR#\(.pull_requests[0].number // "_") \(.head_branch[:35]) \(.created_at[:19]) \(.name[:25]) \(.event)"'
done
```

### Audit script (zero-side-effects guarantee, weekly)

```bash
# scripts/phase2c_weekly_audit.sh
# For every repo touched this week, verify:
#   - 0 commits since first dispatch
#   - 0 new branches matching shadow/cifix/phalanx
#   - 0 new comments on touched PRs
# Output: pass/fail. If ANY fail, immediate halt + investigation.
```

If the audit ever fails, **immediate halt** until root cause is
fixed. No exceptions.

---

## 2. Repo selection strategy

### Watchlist composition

Maintain `config/phase2c_watchlist.txt`. Target: **20–25 repos** by
end of Phase 2c. Today: 11.

Categories the watchlist must cover:

| Category | Examples | Rationale |
|---|---|---|
| **Web framework** | tornadoweb/tornado, encode/httpx, aio-libs/aiohttp, pallets/flask | High PR turnover, network-shape bugs |
| **Linter / formatter** | psf/black, pylint-dev/pylint, astral-sh/ruff (Rust core OK as long as Python wrappers tested) | Common dep-update failures |
| **Testing tools** | pytest-dev/pytest, tox-dev/tox, pre-commit/pre-commit | Meta-proof; high signal |
| **Doc tools** | sphinx-doc/sphinx, mkdocs/mkdocs | Pure-Python; toctree-class bugs |
| **Type system** | python/mypy, pylint-dev/astroid | Bootstrap / metaprogramming bugs |
| **Async libraries** | agronholm/anyio, encode/uvicorn | Async edge cases |
| **Utility libraries** | python-attrs/attrs, hynek/structlog | Pure-Python; high contrib volume |
| **Build tools** | python-poetry/poetry, pypa/setuptools, astral-sh/uv | Workflow drift; install-shape bugs |
| **HTTP clients** | urllib3/urllib3, kennethreitz/requests | Wide deployment; many forks |

### Add-rate

Add 1 new repo per week until 20–25, then hold. New repo onboarding
cost: 1 SQL `INSERT INTO ci_integrations`, then it's in rotation.

### Rotation rule

Each week, pick attempts spanning **≥4 distinct repos**. Don't run
5 entries on the same repo unless intentionally testing a retry
chain (like F2's NM1→NM2→NM3→NM4 progression).

### Skip patterns

- **Scheduled-only failures** (e.g., nightly cron jobs that flake) —
  defer; they're noise.
- **Pure-infra failures** (Codecov upload timeout, GitHub Actions
  outage, network DNS hiccup) — defer unless intentionally testing
  SAFE_ESCALATE on env-drift.
- **Workflow_run_ids older than 7 days** — too stale for TL context;
  PR/main has drifted.

---

## 3. Failure diversity strategy

### Archetypes observed in the 21-row ledger

| Archetype | Count (latest per workflow) | Observation |
|---|---|---|
| Env drift / dep version mismatch | 3 (pylint astroid, tornado pycurl, poetry cffi) | Most common — and most often SAFE_ESCALATE |
| Platform guard missing | 1 (anyio AF_UNIX on Windows) | Underrepresented; deliberately recruit |
| Workflow misconfiguration | 1 (poetry-plugin-export deps drift) | Underrepresented |
| Truncated CI log | 2 (urllib3, psf/black) | Common failure of TL grounding |
| Missing CI tool | 1 (pre-commit dart) | Underrepresented |
| Self-inflicted regex revert (control) | 2 (humanize Path 1, inflect Path 3) | Synthetic baseline |
| Sandbox-setup infra | 2 (sphinx, aiohttp) | Currently FAILED_SANDBOX_SETUP — pattern fixes shipped along the way |

### Active recruitment

Each week, **at least 1 entry must target an underrepresented
archetype**. Track distribution after each run; if any archetype is
<5 occurrences by entry 50, prioritize it explicitly.

Specific archetypes to deliberately seek:

- **Type-error PRs** that fail mypy/pyright (currently 0)
- **Test-snapshot regressions** (e.g., pytest-snapshot, syrupy)
- **API-deprecation cleanup PRs** that break callers
- **Multi-file refactor** PRs that fail in unexpected files
- **Generator / async-generator** edge cases
- **PR-from-fork** vs same-repo PR (we have a few of each — make
  sure ≥10 of each by entry 50)

### Recipe for finding underrepresented archetypes

Watchlist + GitHub Code Search filters:
- `repo:X type:pr is:open status:failure language:python` — surface
  recent failed PRs
- Filter by PR title patterns (`refactor:`, `deprecate:`, `Bump`)
  to bias toward archetype targets

---

## 4. Cost budget projections

### Per-attempt envelope (current)

| Metric | v1.7.3 baseline | Phase 2c projected |
|---|---|---|
| LLM cost | $0.16 / attempt | $0.20 (15% buffer) |
| Wall-clock | 3.3 min / attempt | 4 min (15% buffer) |
| Sandbox provisions | 1 / attempt | 1 / attempt |

### Phase 2c total budget

- **8 weeks × 12.5 attempts/week × $0.20 = $20** baseline LLM spend
- **Buffer for retries** (failed runs that need re-dispatch after
  fixes): +30% = **$26 ceiling**
- **Cost cap per attempt** (operator-side): hard-stop any single run
  >$5 (would indicate runaway TL turn cap or budget bug)

### Wall-clock budget

- **8 weeks × 12.5 × 4 min = 6.7 hours** of run-time across the phase
- Operator time: **~16 hours total** (2 hr/wk × 8 wks)

### Stop conditions (per spec, lock these)

Halt phase 2c **immediately** if any of:

- Any repo mutation (commit, branch, comment, PR open)
- Two consecutive runs end with `phalanx_verdict='SHIPPED_PROPOSED'`
  whose proposed_patch diverges materially from the maintainer's
  actual fix shape (false-ship-equivalent in shadow mode)
- Cumulative phase cost exceeds $50 (2× ceiling)
- Three consecutive FAILED_INFRA on different repos (suggests an
  orchestration regression, not a per-repo issue)

---

## 5. Metrics that matter

### Primary metrics (track weekly)

| Metric | Source | Why it matters |
|---|---|---|
| **Latest-per-workflow SAFE_ESCALATE rate** | `phalanx-shadow metrics --by-workflow` | The architecture-safety-win signal. Should hold ≥75% as N grows. |
| **False-ship count** | Manual review of every SHIPPED_PROPOSED row vs maintainer fix | MUST stay 0. Any non-zero halts the phase. |
| **Side-effect count** | Weekly audit script | MUST stay 0. |
| **Per-archetype hit rate** | Cross-tab archetype × verdict | Identifies which bug shapes Phalanx handles vs not |
| **Cost trajectory** | $/attempt over time | Should stay flat ~$0.20 ± $0.10 |
| **Wall-clock trajectory** | min/attempt over time | Should stay flat ~4 min ± 1 min |

### Secondary metrics (weekly review)

- **FAILED_INFRA breakdown** (timeout / worker_hang / sandbox_setup
  / sandbox_cleanup) — flat or declining means infra is healthy
- **Calibration distribution** — TL confidence histogram on
  SAFE_ESCALATE entries; >50% confidence with SAFE_ESCALATE verdict
  signals calibration drift
- **Tool-call distribution** — TL turns per attempt. >12 turns avg
  (out of 15 cap) signals TL is straining
- **Retry effectiveness** — when a fix is shipped and the same
  workflow_run_id is re-dispatched, does the retry progress further?
  Append-mode makes this measurable directly

### Recurring archetype metrics

For each named archetype (env drift, platform guard, workflow
misconfig, truncated log, missing tool), track:

- N entries
- SAFE_ESCALATE rate within archetype
- TL confidence median within archetype
- Sandbox setup success rate within archetype

By entry 100, expect 5–10 distinct archetypes with N≥5 each.

---

## 6. Thresholds

### "Ready for private beta"

Conditions, ALL required:

| Criterion | Threshold |
|---|---|
| Total ledger attempts | ≥ 50 |
| Unique workflow_run_ids | ≥ 35 |
| Unique repos touched | ≥ 15 |
| Latest-per-workflow SAFE_ESCALATE + SHIPPED_PROPOSED | ≥ 75% |
| False-ship count | **0** |
| Repo side effects | **0** |
| Distinct archetypes covered | ≥ 5 |
| Weeks of stable operation | ≥ 4 |
| Cost per attempt (median) | < $0.40 |
| Time per attempt (median) | < 6 min |

If all met: enable a small private beta program — invite 3–5
maintainers to opt their repos in for shadow-only observation +
weekly diagnostic emails. NO write access yet.

### "Ready for GitHub App write access"

Substantially stricter — write access is the credibility gate that
takes a year to recover from if violated.

| Criterion | Threshold |
|---|---|
| Total ledger attempts | ≥ 200 |
| Unique workflow_run_ids | ≥ 150 |
| Unique repos touched | ≥ 25 |
| Maintainer-confirmed-correct diffs (SHIPPED_PROPOSED) | ≥ 10 |
| Maintainer-confirmed-correct diffs across distinct archetypes | ≥ 5 |
| Latest-per-workflow SAFE_ESCALATE + SHIPPED_PROPOSED | ≥ 80% |
| False-ship count | **0** |
| Repo side effects (cumulative across all phases) | **0** |
| Weeks of stable operation post-private-beta | ≥ 8 |
| Maintainers actively requesting write access | ≥ 3 distinct |

The "maintainer-confirmed-correct" count is the load-bearing
criterion. SHIPPED_PROPOSED with maintainer agreement on the diff
is the only measurement that proves "Phalanx wouldn't have falsed-
shipped here." Without it, all other metrics are circular.

---

## 7. Minimum operational proof for write access

The single hardest gate. Failing it once is unrecoverable for
months. Six conditions, ALL required:

1. **Zero false ships across all phases.** SHIPPED_PROPOSED diff
   matches maintainer's actual fix shape (functional equivalence,
   not exact bytes) on ≥ 10 distinct cases reviewed by ≥ 2 humans.

2. **Zero git mutations on any monitored repo across the entire
   v1.7.x and v1.8.x history.** This is the easiest criterion to
   verify but the hardest to recover from if violated.

3. **At least 3 distinct repo maintainers** have reviewed Phalanx's
   ledger entries on their repo and explicitly endorsed the
   diagnostic quality (written agreement, not implicit).

4. **Architecture refusal rate has held ≥ 75%** across the most
   recent 3 weeks of new entries (no recent regression).

5. **Failure-class distribution is stable** — no surge of new
   FAILED_INFRA modes in the most recent month. Indicates the
   pattern-fix arc has reached steady state for the existing
   repo set.

6. **Engineer step-interpreter audit log** shows zero `commit` or
   `push` step executions in shadow runs across the entire
   ledger. This is enforced by the engineer short-circuit
   (commit `3d4a19c`) and verified end-to-end by Task.output
   evidence per run.

Until all six are met, **write access stays disabled**.

---

## 8. Acceptable vs dangerous failure classes

### Acceptable (architecture working as designed)

| Class | Why acceptable |
|---|---|
| `SAFE_ESCALATE` (any sub-case) | Architecture refused to ship insufficient evidence |
| `FAILED_SANDBOX_SETUP` | Provisioner correctly couldn't establish a verifiable env. No code shipped. |
| `FAILED_INFRA_TIMEOUT` (commander watchdog) | Run-level cap hit; no partial code shipped. Investigate the watchdog isn't masking a real bug. |
| `FAILED_INFRA_WORKER_HANG` (stuck-task detector) | Heartbeat caught a hung worker; cleanup ran. |
| `FAILED_TL` (no fix_spec emitted) | TL refused to fabricate. Same architecture-refusal property. |
| `FAILED` (no failure_class) — pre-hardening Celery hang only | Historical artifact. Should not appear on new runs post-`v1.7.3`. |

### Dangerous (any occurrence triggers immediate halt + investigation)

| Class | Why dangerous |
|---|---|
| **`SHIPPED_PROPOSED` with maintainer-disagreeing diff** | Would be a false ship if write access were enabled. Halt and re-examine calibration. |
| **Any git commit, branch, comment, or PR open on a monitored repo** | Architectural property violated. Worst-case scenario. |
| **`SHIPPED_PROPOSED` returned despite shadow_mode=True** | Engineer short-circuit failed. Would indicate the audit fixes regressed. Halt immediately. |
| **`FAILED` with `failure_class IS NULL` on a recent run** | Classifier missed an infra defect — same as the Phase 2a E2/E5 mis-classification. Halt and investigate. |
| **TL emitted at confidence ≥ 0.7 on a SAFE_ESCALATE archetype (env drift, truncated log)** | Calibration drift; the calibration validator would normally catch this. If it lands as SHIPPED_PROPOSED at high confidence on a non-deterministic shape, that's a real safety regression. |

The `SHIPPED_PROPOSED` cases need the most attention. Every single
one must be reviewed by a human against the maintainer's actual fix
before the entry is treated as ground truth.

---

## 9. Data we still do NOT have

Honest catalog of what the v1.7.3 ledger leaves unanswered:

| Gap | Why it matters | When to fill |
|---|---|---|
| **Hit rate at N ≥ 30 SHIPPED_PROPOSED** | Currently 1 SHIPPED_PROPOSED in the ledger (inflect Path 3). Statistical significance requires N ≫ 1. | Phase 2c via curated repo additions where we expect SHIPPED outcomes |
| **Maintainer feedback on TL diagnoses** | Without this, "11 SAFE_ESCALATE with correct diagnosis" is our self-assessment, not third-party validation. | Phase 2c week 4 onward — pick maintainers who'll respond to a brief comparison email |
| **Multi-iteration SHIPPED runs** | Architecture supports iter-2 with REPLAN priors (`v1.7.2.7`) but no ledger entry exercised it. | When a real-world bug needs iter-2 — wait for it; don't synthesize |
| **Performance under sustained concurrent load** | Currently runs are sequential. Beta would have ≥10 concurrent runs. | Pre-beta load test (separate workstream, not part of Phase 2c) |
| **Long-tail repo coverage** | 25 repos is still a tiny slice of the Python OSS surface. | After private beta — let maintainers self-onboard |
| **Per-archetype calibration accuracy** | We see ~70% of TL ESCALATE come at confidence 0.0 — is that calibrated correctly, or under-confident? | Track confidence histograms across N≥50 |
| **Effect of repo size / language on TL token usage** | Currently $0.16 average; large repos may push 2-3x higher | Track tokens per repo across N≥50 |
| **Recovery from real CI infrastructure outages** | Hardening (heartbeat / detector / cleanup) tested via fault injection only | Wait for one to happen organically — don't synthesize |
| **Behavior on PRs from authors with prior bad commits** | Currently no author-level tracking | After private beta if it becomes a need |

---

## 10. Pattern-fix discipline — only on repeated evidence

**The pattern-fix arc is closed.** Resist the urge to fix new shapes
as one-offs. The rule going forward:

> **No new pattern fix ships unless ≥ 3 distinct workflow_run_ids
> on ≥ 2 distinct repos exhibit the same shape.**

This forces evidence accumulation before code change. It's the
opposite of Phase 2a/2b's "fix on first sight" pattern, which was
appropriate then because each shape was novel and high-frequency.
At Phase 2c scale, every new fix has a higher false-positive cost.

### Examples of fixes to defer

| Shape | Currently | Defer until |
|---|---|---|
| aiohttp's Cython-in-build-isolation | 1 occurrence | 2nd repo with same shape (e.g., uvloop, frozenlist) |
| sphinx's `uv pip --group` (PEP 735) | 1 occurrence | 2nd repo (likely ≥1 other repo will adopt PEP 735) |
| Truncated CI log handling | 2 occurrences (urllib3, black) | 3rd occurrence — consider extending TL prompt to handle truncation gracefully |
| C-extension repos requiring Cython at build time | 1 occurrence (aiohttp) | 2nd occurrence |
| Repos with private dep registries | 0 occurrences | When it happens |

### Exception: data-recording fixes ship anytime

Bug-fixes to the ledger / classifier / cost tracking / observability
ship immediately. These don't risk false negatives on real diagnoses;
they make the ledger more accurate. Same rule applied during Phase
2a/2b.

### Discipline check

After every batch of 10 entries, audit the ledger for repeated
shapes. If a shape has occurred 3+ times AND no fix is in progress,
it goes on the next sprint queue. Otherwise, no new fixes — just
observation.

---

## 11. The single most important guarantee

**Zero side effects on monitored repos.**

This is the load-bearing property. Every other metric is downstream
of it. If we ever ship code to a third-party repo by accident, the
v1.7.3 thesis is dead — no amount of "but our hit rate is X%" will
recover the credibility loss.

Operational defense-in-depth:

1. **Pre-run**: every shadow run sets `Run.shadow_mode=True` AND
   `ci_context.shadow_mode=True`. Engineer short-circuit verifies
   both. Tests lock the engineer can't push under shadow_mode.

2. **During run**: every git mutation in the v3 dispatch path is
   either gated by shadow_mode or in a code path not reachable from
   v3 dispatch. Audit confirmed `3d4a19c`.

3. **Post-run**: weekly audit script queries GitHub API for every
   monitored repo:
   - 0 commits since first dispatch
   - 0 new branches
   - 0 new comments on touched PRs
   - 0 new PRs opened by us
   Any non-zero ⇒ immediate halt.

4. **Continuous**: `Task.output` for every engineer task across the
   entire ledger contains either `committed: false` (shadow path)
   or no `commit_sha` field. Spot-check 5 entries/week.

This is the only criterion that fails to single-failure tolerance.
Everything else accumulates evidence; this one is binary.

---

## 12. What Phase 2c is NOT

Explicitly out of scope to keep operational discipline:

- ❌ Auto-poller for workflow_run discovery (manual is fine at this scale)
- ❌ Maintainer dashboard / UI (the ledger IS the dashboard)
- ❌ Slack integration (manual run output is enough)
- ❌ Multi-language coverage (Python only; v1.8 work)
- ❌ New agent roles (5 is enough)
- ❌ Architecture rewrites (post-v1.8 if needed)
- ❌ Marketplace listing / GitHub App registration (post-write-access proof)
- ❌ Pattern-fix sprints in response to single occurrences
- ❌ Synthetic shadow runs (only real failures from the watchlist)

If any of the above feel necessary mid-phase, that's signal the
phase isn't really "operational observation" any more — it's
become a coding sprint. Halt and reset scope.

---

## 13. Bottom line

Phase 2c is **8 weeks, 12–15 attempts/week, ~$26 budget, 16 operator
hours, zero new code unless operationally necessary**. The deliverable
is the 100-row ledger + the per-week metrics dashboard built from
existing CLI exports, not new infrastructure.

The architecture is done for now. The data is the next thing to
collect. The ledger is the proof.
