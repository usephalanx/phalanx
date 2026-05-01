# CI Fixer v3 — roadmap to GitHub App marketplace

**Audience**: tech lead / product owner / future-self.
**Purpose**: capture the journey, current state, gaps, and the path forward in one place.
**Date**: 2026-05-01.
**Companion doc**: [v15-to-7p5-sprint.md](v15-to-7p5-sprint.md) — the implementation spec for the next 7 days.

---

## Executive summary

CI Fixer v3 is a multi-agent pipeline (Tech Lead → Engineer → SRE) that detects failing GitHub Actions CI, diagnoses the root cause, applies a code fix, and pushes the fix to the customer's PR branch. v1.5.0 (shipped 2026-05-01) is **architecturally sound** and **proven on external Python** (humanize: lint + test_fail + flake all SHIP cleanly), but is **not yet battle-tested for production marketplace launch**.

Honest current quality bar: **3.5/10**. The architecture is at 7; coverage / scale / wild-bug-proof drag the average down. Goal for next 7 days: **7.5/10** — beta-ready for 3-5 invite-only maintainers. Marketplace public listing follows in week 3-4 (GitHub's review window is the rate-limiting step).

---

## Milestones shipped

### v1.0 - v1.3 (prior sessions)
- v2 architecture: single-agent loop with verification gate
- 4-cell internal Python testbed (`usephalanx/phalanx-ci-fixer-testbed`)
- 8 canary bugs found + fixed in 1-week iterative cycle
- v2 baseline: ~$0.91 total cost across 4 cells, all committed

### v1.4.0 (2026-04-28) — bug #11/#12 webhook hardening
- A1: bot-author filter via fix_commit_sha lookup
- A3: idempotency key via check_suite_id + DB unique partial index
- B2: pg_try_advisory_xact_lock per (repo, pr_number)
- Architectural docs: webhook-coordination.md, agentic-sre.md
- Internal Python: 4/4 SHIP unchanged (regression check)

### v1.4.1 (2026-04-30) — bug #14 GHA expression skip
- SRE verify skips workflow commands containing `${{ ... }}` literals
- Unblocked humanize test_fail/flake cells (would otherwise hit "Bad substitution")

### v1.4.2 (2026-04-30) — bug #15 cache replay
- sre_setup_cache replay actually executes install steps on fresh sandbox + re-verifies tokens
- First external SHIP: humanize lint cell (PR #8)

### v1.5.0 (2026-05-01) — agent contracts (TL → Engineer)
- New TL output schema: `verify_command` + `verify_success` matrix + `self_critique` evidence
- TL prompt rewrite: 5 worked examples for verify_command choice (DEFAULT, DELETE TEST, RENAME, ENV/CONFIG, VERSION BUMP)
- Engineer reads matcher: exit_codes list + stdout_contains + stderr_excludes
- Backwards compat: pre-v1.5 fix_specs without verify_command fall back to v1.4.x behavior
- **Closed bug #16 architecturally** (no per-pytest exit-code patches)
- 17 new tier-1 tests (10 parser + 7 matcher); 155 v3 tests total green

### Canary results on v1.5.0 (the architectural proof)
- **Internal testbed: 4/4 SHIP** (lint, test_fail, flake, coverage) — regression unchanged
- **External humanize: 3/3 viable cells SHIP**:
  - lint (PR #10): SHIPPED, src/humanize/lists.py
  - test_fail (PR #11): SHIPPED, tests/test_lists.py — TL chose `verify_command=pytest tests/test_lists.py` (broader than failing_command's selector)
  - flake (PR #12): SHIPPED, tests/test_lists.py — same TL discipline
  - coverage: not viable on humanize (no `--cov-fail-under` gate); structural limitation, not v3 limitation

---

## Current state — axis-by-axis (10 = marketplace-ready, 1 = prototype)

| axis | now | reasoning |
|---|---|---|
| Reliable fix-rate (real-world wild bugs) | **3** | only synthetic bugs proven; zero wild-fix evidence |
| Reliable fix-rate (in-distribution synthetic) | **7** | 6/6 in last canary; pattern-isolated |
| Multi-language coverage | **1** | Python only; JS/Java/Go is aspirational |
| Battle-tested at scale | **1** | <50 lifetime runs; never tested >2 concurrent |
| Observability | **2** | grep + manual DB queries; no dashboards |
| Self-correction | **3** | iter-2 (one round); prompt-driven self_critique not validated |
| Safe failure modes | **7** | escalation paths exist; no destructive actions; **stuck-run reaper missing** |
| Cost discipline | **6** | per-LLM caps work; no per-run cap = runaway risk |
| Speed | **7** | 1-3 min typical, fine for use case |
| Trust / wild adoption | **0** | one external opt-in repo (humanize); zero marketplace footprint |
| Architectural cleanliness | **7** | DAG + contracts + fix discipline are genuinely clean |

**Weighted current score: ~3.5/10.**

---

## Gap audit (what's actually in the codebase RIGHT NOW)

Concrete findings from grep'ing the repo:

| gap | code reality | actual delta to fix |
|---|---|---|
| **Self-critique enforcement** | Parser preserves `self_critique` field but does NO validation. Prompt says "you must self-critique" — LLM can fake all 3 booleans. | New tool `validate_self_critique` with deterministic checks per boolean; commander gate that rejects fix_spec on any false |
| **Stuck-run reaper** | `phalanx.maintenance.tasks.check_blocked_runs` runs every 5 min via redbeat. **Body is a TODO stub** — `log.info("stub_noop")` | Replace stub with real query: kill Run rows in EXECUTING/VERIFYING with `updated_at < now - 30min` |
| **Per-run cost cap** | `Run.tokens_used` field exists. Each agent reports `tokens_used` in AgentResult. **Commander never aggregates.** No cap enforcement anywhere. | Aggregator hook in `_dispatch_next_task`: `sum(tasks.tokens_used) * cost_per_token > $1` → abort run |
| **Concurrency** | 3 worker pools (ci-fixer=4, sre=2, normal=2). Shared Postgres + Redis + Docker daemon. | Stress harness: fire N=20 concurrent PRs against testbed; observe failure modes; cap if needed |
| **Observability** | structlog JSON logs only. **Zero SQL views**. Flower exists but is celery-internal, not v3-quality-focused. | 5 SQL views (terminal_state_24h, fix_rate_by_category, cost_per_run, false_positive_rate, reaper_kills) + minimal HTML dashboard + Slack alert |
| **GitHub App vs webhook** | Today: PAT-based per-repo integration in `ci_integrations`, hardcoded by repo_full_name. Each new repo requires manual DB row + webhook setup. | Multi-tenant refactor: `github_app_installations` table, OAuth callback, installation tokens (1h TTL), credential resolver replacing hardcoded PAT lookup |

**Key architectural insight from this audit**: every gap is a *known unknown* — we have the table, the schedule, the field, just not the implementation. The runway from prototype to beta-ready is about *plumbing the existing scaffolding*, not designing new architecture.

---

## Goals — definition of "ready"

### Beta-ready (target end of week 1)
- [ ] Self-critique enforced (catches LLM hallucinations at validator-level)
- [ ] Stuck-run reaper working (kills runs >30min in EXECUTING/VERIFYING)
- [ ] Per-run cost cap ($1.00/run hard abort)
- [ ] Path 1 wild-bug proof (real humanize fix `a47a89e` re-derived)
- [ ] 5 SQL observability views + HTML dashboard + Slack quality alerts
- [ ] Stress test passes N=20 concurrent (≥90% SHIPPED, p95 ≤15min)
- [ ] GitHub App: install link works on a fresh 3rd test repo

### Marketplace-public-launch-ready (target week 3-4)
- [ ] 2-4 weeks of soak time on humanize + 3-5 beta repos with low false-positive rate
- [ ] Privacy policy + ToS finalized (legal review)
- [ ] Billing / pricing tier decided (free? paid? freemium?)
- [ ] Public marketing site / install page
- [ ] Customer support channel (email or Discord)
- [ ] App icon, branding, marketing copy
- [ ] GitHub App marketplace review submission (5-14 day review window)

### "Battle tested for Python at 9/10" (target month 2-3)
- [ ] ≥100 wild fixes shipped across diverse repos
- [ ] False-positive rate <2% over 30 days
- [ ] Cost per fix <$0.30 average
- [ ] At least one well-known OSS maintainer endorsement
- [ ] At least one customer of v1.7+ feature class (multi-language or escalation policies)

### "Multi-language at 9.5+/10" (target month 3-6)
- [ ] env_detector full Node/TS detection (package.json, package-lock, pnpm-lock)
- [ ] env_detector full Java detection (pom.xml, gradle)
- [ ] env_detector full C# detection (csproj, sln)
- [ ] One paying customer per non-Python language

---

## Path forward — week-by-week

### Week 1 (this week) — prototype → beta-ready (3.5 → 7.5)
See [v15-to-7p5-sprint.md](v15-to-7p5-sprint.md) for daily breakdown. Six phases:
- Sun: Phase 0 — spec lock (this doc + companion)
- Mon: Phase 1 — self-critique validator
- Tue: Phase 2 — reaper + per-run cost cap
- Wed: Phase 3 — wild-bug proof (Path 1)
- Thu: Phase 4 — observability surface
- Fri: Phase 5 — concurrency stress
- Sat: Phase 6 — GitHub App + beta-ready

### Weeks 2-3 — invite-only beta + soak (7.5 → 8.0)
- Recruit 3-5 friendly maintainers
- Personal install + first-day support per maintainer
- Daily monitoring of dashboards + Slack alerts
- Bug fixes from real-world signal — but bug VOLUME should be low because Phases 1-5 cover the known classes
- Iterate on TL prompt based on observed failure modes
- Document new bug classes in retrospectives

### Week 4 — marketplace submission prep (8.0 holding)
- Privacy policy + ToS finalization
- Marketing site / install landing page
- Pricing decision (recommend free for first 6 months to drive adoption)
- App icon + branding
- Submit for GitHub App marketplace review (5-14 day window)

### Months 2-3 — public marketplace + Python hardening (8.0 → 9.0)
- Marketplace listing live
- Inbound install flow tested at scale
- Python-only constraint communicated explicitly
- Per-class diagnostics (bugs caught by class, fix rate by class)
- Cost optimization (memoize more aggressively, prompt compression)

### Months 3-6 — multi-language expansion (9.0 → 9.5+)
- env_detector for Node + TS + Java + C#
- Per-language testbed repos
- Per-language canary regression suites
- Marketing for non-Python launches

---

## Open decisions (you own these)

1. **Beta launch this week or next week?**
   - This week ships at 7.5 if Phase 0-6 nail their exit criteria.
   - Next week buys an extra soak cycle for Phase 1-5 to mature.
   - Recommendation: **next week** if you want stronger evidence; **this week** if go-to-market urgency dominates.

2. **Public marketplace timing?**
   - Earliest credible: week 4-5 (after 2 weeks beta soak).
   - Recommended: week 6-8 (after 4 weeks beta + bug iteration).

3. **Pricing for marketplace launch?**
   - Free-tier-only (drives adoption, pure cost): recommended for first 6 months.
   - Paid-only (monetizes immediately, slower adoption): not recommended for unproven product.
   - Freemium (free for OSS, paid for private repos): mainstream choice but adds billing complexity.

4. **Multi-language scope?**
   - Python + Node + TS in v1.7 (Q3): high-leverage, ~3 weeks each language.
   - Python + Node only forever: niche but defensible.
   - Recommendation: Python + TS as the next two; Java + C# follow if traction.

5. **Self-critique scope creep?**
   - Phase 1 is TL only. Engineer + SRE self-critique deferred to v1.7.
   - Risk: engineer-side hallucinations remain possible. Mitigated by iter-2 + reaper.
   - Recommendation: ship beta with TL-only self-critique; expand later.

6. **Coverage cell on external (the 4th cell on path B)?**
   - humanize CI doesn't gate coverage; can't naturally trigger.
   - Either: find a different external repo with coverage gate (e.g., requests, attrs) for canary, OR document as "test rig limitation, not v3 limitation"
   - Recommendation: document; pursue different external repos in beta naturally.

---

## Risk register (cross-cutting)

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| Phase 1 validators reject legit fix_specs (false negatives) | med | high | tier-1 corpus tests both directions; tunable thresholds |
| Phase 3 v3 produces wrong fix on humanize tz revert | med | med | accept as data; lower exit bar; ship at 7.0 instead of 7.5 |
| Phase 5 N=20 reveals concurrency limit < 5 | low | high | ship with documented `max_concurrent=N_safe` policy |
| Beta maintainer's branch gets a destructive commit | low | very high | branch protection + PR-only commits + reaper + cost cap minimize blast radius |
| GitHub App marketplace review takes 14+ days | high | low | start review submission week 4, not week 7 |
| Bug surfaced in beta requires architectural fix | med | high | invite-only beta limits blast radius; bugs become roadmap input |

---

## What success looks like at end of sprint (binary)

- 7/7 Definition-of-Done items in the sprint spec ✅
- Dashboard URL bookmarked, all 5 views populating
- 3-5 maintainer recruitment list with names, repos, target install dates
- Privacy policy + ToS draft committed (markdown is fine; not legal review yet)
- Slack alert tested + receiving messages
- Path 1 result documented (success or instructive failure)
- This roadmap doc + companion sprint spec on origin

If 5+/7 ✅: ship beta to 1-2 maintainers Monday week 2; full beta by Wed.
If 3-4/7 ✅: extend sprint by 3-5 days; do not ship yet.
If <3/7 ✅: re-evaluate scope; possibly ship at 6.5/10 with explicit limitations communicated.

---

## Appendix — bug retrospective (for posterity)

| bug # | class | fix release | architectural lesson |
|---|---|---|---|
| #1-#8 | canary cycle (v2) | v2.x | "v3 forgot something v2 bootstrap does" |
| #9 | SRE workflow YAML parser | v1.4.0 | Shell line-continuations in `run: \|` blocks; needed regex collapse |
| #10 | Engineer Sonnet 180s timeout | v1.3.41 | Multi-file code-gen needs >180s; bumped to 300s |
| #11 | Webhook race / no per-PR coordination | v1.4.0 | Three-layer dedup (A1 bot-filter + A3 idempotency + B2 advisory lock) |
| #12 | Iter-2 TL stuck IN_PROGRESS | v1.4.0 (resolved indirectly via #11 fix) | Parallel-run interference; eliminated by per-PR coordination |
| #13 | Engineer guard order (confidence vs failing_command) | v1.4.0 (latent) | When TL says "no code fix possible", surface low_confidence cleanly, not "missing failing_command" |
| #14 | SRE verify runs `${{ matrix.* }}` literals | v1.4.1 | GHA expressions don't expand outside Actions; skip them in verify |
| #15 | Cache replay returned READY without re-installing | v1.4.2 | Cached plan is metadata; replay must EXECUTE the install commands |
| #16 | Engineer rigid exit-0 verify (broke "delete test" fixes) | v1.5.0 | Architectural: TL must specify HOW to verify (verify_command + verify_success matrix) |

**Pattern**: each bug surfaced from canary, was diagnosed, surgically fixed with discipline (understand → plan → surgical change → verify → strict output template), test added to prevent regression. The cycle averaged 1-2 bugs per session over the multi-session arc. v1.5.0 shifted from per-bug fixes to architectural contract refactor — closed an entire class.

---

## TL;DR for stakeholders

- **Where we are**: 3.5/10. Architecturally sound. Proven on synthetic bugs in 1 external repo. Not battle-tested.
- **Where we're going next 7 days**: 7.5/10. Beta-ready for 3-5 invite-only maintainers. NOT marketplace-ready.
- **Where we're going next 4 weeks**: marketplace-public via GitHub App listing. Python-only.
- **Where we're going months 2-3**: 9/10 via real-world adoption + bug iteration.
- **Where we're going months 3-6**: 9.5/10 via multi-language expansion.

The plan is not aggressive; it's honest. The only acceleration available is reducing safety margins, which we shouldn't do for a product that ships commits to other people's branches.
