# Phase 2c — Week 1 Operational Report

**Date:** 2026-05-11
**Window:** 2026-05-08 → 2026-05-11
**Mode:** shadow_mode=True, zero side effects
**Entries observed:** 13 dispatches across 9 distinct repos
**Framing:** "Can Phalanx safely operate on unknown repos?" — safety > success rate.

---

## 0. Entry-level summary

| #     | Repo                   | Workflow archetype          | Verdict          | Conf | Secs | Cost  | Notes                                                                  |
| ----- | ---------------------- | --------------------------- | ---------------- | ---- | ---- | ----- | ---------------------------------------------------------------------- |
| W1.1  | sphinx-doc/sphinx      | docs/build (Lint)           | FAILED_SANDBOX   | —    | 47   | $0    | uv `--group` flag, image lacks                                         |
| W1.2  | sphinx-doc/sphinx      | docs/build (CI linkcheck)   | SAFE_ESCALATE    | 0.0  | 274  | $0.80 | truncated linkcheck log; TL refused to commit                          |
| W1.3  | python-poetry/poetry   | packaging (init-author)     | SAFE_ESCALATE    | 0.0  | 170  | $0.36 | cffi workflow drift; TL flagged env-side, not code-side                |
| W1.4  | agronholm/anyio        | async/runtime               | SAFE_ESCALATE    | 0.0  | 183  | $0.59 | test_tcp_listener port 54321 collision; TL refused without isolation   |
| W1.5  | aio-libs/aiohttp       | packaging (speedups extra)  | FAILED_SANDBOX   | —    | 62   | $0    | `pip install [speedups]` Cython mask.pyx                               |
| W1.6  | aio-libs/aiohttp       | dependabot bump             | FAILED_SANDBOX   | —    | 64   | $0    | same speedups archetype                                                |
| W1.7  | pylint-dev/pylint      | linting + dep conflict      | SAFE_ESCALATE    | 0.0  | 184  | $0.39 | astroid env drift                                                      |
| W1.8  | pylint-dev/pylint      | dep conflict (sharper)      | SAFE_ESCALATE    | 0.0  | 213  | $0.43 | astroid 4.2.0b1 vs repo pin 4.2.0b3 — high-quality diagnosis           |
| W1.9  | psf/black              | docs/build (changelog)      | FAILED           | 0.82 | 213  | $0.54 | CHANGES.md required; engineer step_precondition_violated. **ANOMALY: ledger conf=0.82, TL task conf=0.0** |
| W1.10 | tornadoweb/tornado     | flaky/setup (pycurl-seek)   | SAFE_ESCALATE    | —    | 214  | $0.28 | attempt #2; no root_cause captured — data-gap concern                   |
| W1.11 | python/mypy            | **typing**                  | **SHIPPED**      | 0.9  | 245  | $0.63 | `assert isinstance(analyzed, ParamSpecType)` — precise narrowing       |
| W1.12 | python/mypy            | typing (retry shape)        | FAILED_SANDBOX   | —    | 867  | $0    | post-rebuild infra; 867s is anomalous (>4× normal) — see §3            |
| W1.13 | pytest-dev/pytest      | linting (fix/getoption)     | FAILED_SANDBOX   | —    | 122  | $0    | post-rebuild infra; same archetype as broader sandbox flakiness        |

**Verdict distribution:** SHIPPED_PROPOSED 1 · SAFE_ESCALATE 6 · FAILED (TL/engineer) 1 · FAILED_SANDBOX_SETUP 5

**Cost:** $4.02 total over 13 dispatches (mean $0.31, max $0.80). The $25 run-cap is comfortably observed.

**Archetype coverage** (target: 10):

| Archetype                     | Hit?    | Source(s)                          |
| ----------------------------- | ------- | ---------------------------------- |
| typing                        | ✅      | W1.11 (SHIPPED), W1.12             |
| async/runtime                 | ✅      | W1.4                               |
| packaging/install             | ✅      | W1.3, W1.5, W1.6                   |
| docs/build                    | ✅      | W1.1, W1.2, W1.9                   |
| linting                       | ✅      | W1.7, W1.13                        |
| matrix regressions            | ❌      | not exercised                      |
| dependency conflicts          | ✅      | W1.7, W1.8 (sharp)                 |
| flaky tests                   | ✅      | W1.4 (port collision), W1.10       |
| workflow/setup failures       | ✅      | W1.1, W1.5/6, W1.12, W1.13         |
| multi-platform behavior       | ❌      | only ubuntu jobs observed          |

8/10 archetypes covered. Matrix-regression and multi-platform are open for Week 2.

---

## 1. Operational readiness assessment

**Phalanx is not ready for invite-only beta.** Three issues with different shapes have surfaced in Week 1, and each one independently disqualifies a private-beta launch:

1. **DB durability is unproven** — a single `docker compose up -d` on this machine destroyed the entire Week 1 ledger (W1.1–W1.11) because the previous postgres volume was no longer attached. No automated backup, no recovery path, no audit trail outside the live DB. For a system whose pitch is "we observed real CI failures and saved precise evidence," losing the evidence is fatal.

2. **The bootstrap path is not self-consistent** — a fresh-install `alembic upgrade head` failed on a UUID/VARCHAR FK type mismatch ([alembic/versions/20260321_0001_add_dag_columns.py](alembic/versions/20260321_0001_add_dag_columns.py)) that's been latent since 2026-03-21. Production-style stacks have only ever worked because their DBs predated the bug; nobody had ever genuinely bootstrapped from zero. An invite-only beta means N new dev environments doing exactly that.

3. **Data integrity between ledger and task output is provably inconsistent** — W1.9 (psf/black changelog) shows `shadow_ledger.phalanx_confidence = 0.82` and a root_cause field referencing CHANGES.md, while the corresponding `cifix_techlead` task output records `confidence = 0.0` and `review_decision = ESCALATE`. The two fields are populated through the same pipeline and must not disagree. Until the divergence is explained, every SAFE_ESCALATE and every FAILED verdict on the ledger is suspect.

What **is** working:
- Verdict classifier (4 SAFE_ESCALATE sub-cases + FAILED_SANDBOX_SETUP detector) routes consistently. 6/8 SAFE_ESCALATEs in Week 1 are confidence=0.0 with a non-trivial root_cause — exactly what the classifier was designed to surface.
- Zero side-effect invariant held across all 13 dispatches. No pushes, no commits, no PRs, no comments. Shadow-mode short-circuit is solid at both the v1.6 path and the v1.7 step-interpreter path.
- W1.11 (python/mypy paramspec) produced a precise one-line patch with the correct ParamSpecType narrowing — the SHIPPED_PROPOSED archetype works when the engineer can actually run.
- W1.8 (pylint astroid dep drift) demonstrated diagnosis quality improving over W1.7 — the same archetype produced a sharper, version-specific call-out on retry.

---

## 2. Biggest blockers to invite-only beta

In priority order:

**B1. No DB durability story.** This is the gating blocker. Required before invite-only beta:
- Postgres volume must be backed by automatic daily dumps (off-host).
- The ledger.json export must auto-run after every dispatch — not as a manual `phalanx shadow export` afterthought.
- Bootstrap must verify the volume is the expected one before binding; today, a stale compose project name silently mounts a fresh volume.

**B2. The migration chain is not bootstrap-safe.** Fixed today on [alembic/versions/20260321_0001_add_dag_columns.py](alembic/versions/20260321_0001_add_dag_columns.py), but the broader risk remains: there's no CI step that runs `alembic upgrade head` against a fresh DB on every PR. Until that exists, more latent bugs are likely present in the chain.

**B3. Ledger vs. task output data inconsistency (W1.9).** The `update_with_results` path that populates the ShadowLedger row from the Run terminal state has at least one mode where it writes values that don't match the underlying task output. Investigation in Week 2 must answer:
- Which step writes phalanx_confidence + phalanx_root_cause?
- Why does it diverge from the TL task's recorded `confidence` and `review_decision`?
- Is this the engineer's confidence being recorded after the TL ESCALATEd?

**B4. Sandbox setup is fragile post-rebuild.** W1.12 took 867s before failing (>4× the normal 200s) and W1.13 failed in 122s with no diagnosis. Both ran on the freshly rebuilt local stack. The compose-level sandbox provisioning has not been re-verified end-to-end since v1.7.3's runtime hardening landed — and the existing FAILED_SANDBOX_SETUP detector (which fires on these) shows the failure shape but not the cause. We need:
- A sandbox-bootstrap smoke test that runs on every commit.
- Per-stage logging inside the SRE setup path so FAILED_SANDBOX_SETUP entries carry enough detail to triage without re-running.

**B5. Track-only patterns are accumulating without resolution.** Three patterns now sit below the `≥3 across ≥2 repos` pattern-fix threshold but are growing:
- astroid PyPy crash — 5 occurrences in pylint (still 1 repo).
- aiohttp Cython speedups — 3 occurrences in aiohttp (still 1 repo).
- sphinx uv `--group` — 2 occurrences in sphinx (still 1 repo).
The rule is right (it prevents pattern-fix overfitting on a single-repo quirk), but the operational implication is that aiohttp and pylint can't be dispatched usefully until Week 4 minimum if we keep getting same-repo retries. Suggest: pre-select Week 2/3 repos to deliberately *not* re-dispatch the three patterns above — diversity > volume.

**B6. Diagnosis quality on SAFE_ESCALATE is uneven.** W1.10 (tornado pycurl-seek) finished with `phalanx_confidence=null` and no root_cause in the ledger — yet the run exited cleanly at 214s, $0.28. SAFE_ESCALATE with no diagnosis is operationally the same as silent FAILED. We need a post-condition: every SAFE_ESCALATE row must carry either a root_cause OR a specific reason for the empty diagnosis (e.g. "TL never produced output, run hit cost cap"). The classifier already has 4 sub-cases; the ledger writer doesn't expose which one fired.

---

## 3. Confidence levels

### 3.1 SAFE_ESCALATE reliability: **Medium**

What works: 6/6 SAFE_ESCALATEs (excluding W1.10's empty diagnosis) carried plausible, repo-specific root_cause text. None of them recommended a patch that would have been wrong to ship — that's the bar SAFE_ESCALATE has to clear, and it cleared it.

What doesn't: W1.9's anomaly means the ledger's own ground-truth for what TL said is unreliable. Until B3 is resolved, no SAFE_ESCALATE row can be cited externally as proof of behavior.

Net: the *decision* to escalate is trustworthy this week, but the *recorded reason* for the decision is not auditable yet.

### 3.2 SHIPPED_PROPOSED quality: **Insufficient sample**

n=1 (W1.11, python/mypy). The single sample is good — a precise, minimal, isinstance-narrowing patch on a typing union-attr bug. But you cannot characterize a precision/recall curve from one point. Pre-beta target: at least 5 SHIPPED_PROPOSED rows across ≥3 archetypes before claiming the ship-rate is anything specific. Today's correct claim is "Phalanx has shipped a SHIPPED_PROPOSED that, on inspection, was a correct fix" — not a rate.

### 3.3 Infra stability: **Low**

Week 1 surfaced:
- Compose typo (forge-worker vs phalanx-worker) blocking `docker compose up` — patched.
- Migration chain not bootstrap-safe — patched.
- Postgres volume re-creation on `docker compose up` if compose project name shifts — not patched, documented.
- 2/2 fresh-stack dispatches (W1.12, W1.13) hit FAILED_SANDBOX_SETUP without diagnosis.
- Heartbeat/stuck-task detector worked (no hangs), but cifix-worker container had no healthcheck and ran "unhealthy" by docker's measure — operational signal is weak.

The runtime-hardening shipped in v1.7.3 is doing its job at the *detection* layer (no infinite loops, no zombie tasks). What's not in place is *prevention* at the bootstrap layer.

---

## 4. Minimum additional proof for next milestones

### Trusted private beta (≤5 friendly maintainers)

Must have, in order:
- **5 SHIPPED_PROPOSED entries** across at least 3 archetypes, each independently inspected and confirmed correct. (Today: 1.)
- **Resolution of B3** (W1.9 ledger/task divergence) with a regression test that fails on the original symptom and passes on the fix.
- **Postgres volume durability**: automated daily dumps to a separate disk + a documented restore procedure that has been run end-to-end at least once.
- **`alembic upgrade head` from empty** as a CI step on every PR.
- **Zero FAILED_SANDBOX_SETUP runs missing a structured root_cause** — every sandbox failure must be diagnosable from the ledger alone.

Nice to have:
- Matrix-regression and multi-platform archetypes exercised at least twice each.
- 20+ total dispatches over a 2-week window with infra-stability events trending down (not flat).

### GitHub App onboarding (read-only events)

Must have, on top of private-beta criteria:
- A **per-repo CIIntegration registration flow** that does not require seeding rows directly into postgres. The user-facing setup must work without DB access.
- **PAT scope minimization** — current dev seed used a classic PAT with full `repo` + `workflow` + `admin:repo_hook` scopes. App must operate with the minimal installation permissions (`pull_requests:read`, `checks:read`, `contents:read`, `actions:read`).
- **30+ SAFE_ESCALATE entries** with diagnosis quality consistently rated as "actionable by the maintainer" by an independent reviewer (you).
- **Heartbeat-based queue depth observability** exposed via a status endpoint so a maintainer can ask "is Phalanx healthy right now?" and get a real answer.

### Limited write-access experiments (Phalanx opens a PR draft)

Must have, on top of GitHub App criteria:
- **A documented refusal path** — Phalanx must reject the write attempt if any of: confidence < 0.85, self_critique flagged inconsistency, calibration_failed fired, review_decision != APPROVE. Behavior of this path must be exercised on at least 10 entries that *would have* been refused, with the ledger showing the refusal reason.
- **No FAILED_SANDBOX_SETUP entries in the trailing 30 dispatches** — write-access while the sandbox bootstrap is fragile would put garbage PRs onto real repos.
- **Patch quality reviewer** — a second model (or human) reviews proposed patches before any PR is opened. SHIPPED_PROPOSED is necessary but not sufficient for autonomous write.
- **A blast-radius cap**: at most one open Phalanx PR per repo at a time, automatic close on stale, no force-push, no commits to branches Phalanx didn't create.

---

## 5. What I'd do next (recommendations, not commitments)

1. **Don't run Week 2 dispatches until B1 is resolved.** Losing another ledger erases the cumulative proof you'd be writing this report against.
2. **Re-run W1.10 with logging instrumentation** to find why the SAFE_ESCALATE diagnosis came back empty. That's a different shape from the other 5 SAFE_ESCALATEs and the signal is being swallowed somewhere.
3. **Investigate W1.9 before more dispatches.** Until the ledger row's confidence/root_cause is provably derived from the TL task output, the operational report's numbers are vibes.
4. **Add `alembic upgrade head` to CI on a fresh postgres.** Catches the next 20260321-style bug before it bites a beta tester.
5. **Pre-pick Week 2 repos to skip the three track-only patterns** (aiohttp/pylint/sphinx-uv) so the diversity dimension keeps climbing while pattern-fix discipline stays intact.
