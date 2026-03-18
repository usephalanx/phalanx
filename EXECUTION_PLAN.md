# FORGE — Execution Plan
## Team: 4 Engineers (IC3–IC5) + 1 IC6 | Target: 6 Weeks to Production

---

## Team Assignments

| Agent | Level | Role | Primary Ownership |
|---|---|---|---|
| Morgan | IC6 | Tech Lead | Architecture decisions, PR reviews, IC6 approvals, unblocking |
| Jordan | IC5 | Fullstack Lead | Core engine: state machine, skill engine, memory, agents |
| Alex | IC4 | Frontend Engineer | Slack gateway, API routes, config loader, CLI tools |
| Riley | IC4 | DevOps Engineer | Docker, CI/CD, infra, migrations, observability |
| Sam | IC3 | Backend Engineer | DB models, schemas, Celery tasks, test utilities |

**Principles:**
- IC6 reviews all PRs before merge. No exceptions.
- IC3 output reviewed by IC5 before IC6 sees it.
- Max 2 in-progress tasks per engineer at any time.
- Daily async standup: 3 sentences. What shipped. What's in progress. What's blocked.
- Feature flags gate every new capability — `FORGE_ENABLE_*` in `.env`.

---

## Milestone Overview

```
WEEK 1  ├── M1: Infrastructure Ready        (Days 1–2)
        └── M2: Config + Skills Foundation  (Days 3–5)

WEEK 2  ├── M3: Gateway + Commander         (Days 6–8)
        └── M4: Planning Loop               (Days 9–10)

WEEK 3  ├── M5: Execution Loop              (Days 11–14)
        └── M6: Quality + Ship              (Days 15–17)

WEEK 4  ├── M7: End-to-End + Hardening      (Days 18–20)
        └── M8: Production Deploy           (Days 21–22)  ← MVP SHIPPED

WEEK 5  └── M9: Memory + Learning           (Days 23–27)  ← Phase 2 begins

WEEK 6  └── M10: Reporting + Maintenance    (Days 28–30)
```

---

## MILESTONE 1 — Infrastructure Ready
**Duration:** Days 1–2 | **Owner:** Riley (IC4) + Sam (IC3)

### Goal
Any engineer runs `make setup && make up && make migrate` and has a working,
fully connected local environment with a green health check.

### Tasks

```
TASK                                    OWNER   EST
────────────────────────────────────────────────────
Write Dockerfile (multi-stage)          Riley   2h
Write docker-compose.yml (dev)          Riley   2h
Write docker-compose.prod.yml           Riley   1h
Write nginx config                      Riley   1h
Write .env.example (all vars)           Riley   1h
Write Makefile with all commands        Riley   2h
Write pyproject.toml with all deps      Sam     1h
Write scripts/init-db.sql               Sam     30m
Write alembic/env.py                    Sam     30m
Write forge/db/models.py (all tables)   Sam     4h
Write 001_initial_schema migration      Sam     2h
Write forge/config/settings.py          Sam     1h
Write forge/api/main.py (health check)  Alex    1h
Write forge/observability/logging.py    Riley   1h
Write forge/cache/redis_client.py       Sam     30m
Write forge/queue/celery_app.py         Sam     1h
IC5 review + IC6 sign-off              Jordan   2h
```

### Definition of Done
- [ ] `make setup && make up` runs without errors
- [ ] `make migrate` applies all migrations cleanly
- [ ] `GET /health` returns `{"status": "ok", "db": "connected", "redis": "connected"}`
- [ ] Flower UI accessible at `http://localhost:5555`
- [ ] pgAdmin accessible at `http://localhost:5050`
- [ ] `make test` runs (even if 0 tests — runner works)
- [ ] `make lint` passes with 0 errors
- [ ] IC6 has reviewed and approved all infrastructure code

### Go / No-Go Criteria
> **GO if:** All engineers can boot the stack from a fresh clone in under 5 minutes.
> **NO-GO if:** Any service fails health check, migrations error, or build takes > 10 minutes.

---

## MILESTONE 2 — Config + Skills Foundation
**Duration:** Days 3–5 | **Owner:** Jordan (IC5) + Alex (IC4)

### Goal
Team YAML loaded → agents instantiated with correct IC levels and skill sets.
Skills loaded from registry → filtered by load strategy. Skill gap detected before task.
`validate_config.py` and `validate_skills.py` pass with zero errors.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write config/schema/*.schema.json             Alex    2h
  (team, project, guardrail, workflow, skill)
Write forge/config/models.py                  Alex    3h
  (Pydantic models for all config types)
Write forge/config/loader.py                  Alex    2h
  (YAML loader + JSON Schema validation)
Write forge/config/validator.py               Alex    1h
Write config/teams/website-alpha/team.yaml    Jordan  2h
Write config/projects/acme-website/project.yaml Jordan 1h
Write config/guardrails/website-alpha/        Jordan  2h
Write config/workflows/web-feature/           Jordan  1h
Write config/domains/web/domain.yaml          Jordan  1h
Write skill-registry/registry.yaml            Jordan  1h
Write 8 core skills (YAML):                   Jordan  4h
  - read_codebase
  - build_react_component
  - design_rest_api
  - write_unit_test
  - decompose_epic_to_tasks
  - conduct_code_review
  - escalate
  - checkpoint_progress
Write forge/skills/registry.py                Jordan  2h
Write forge/skills/loader.py                  Jordan  2h
Write forge/skills/engine.py                  Jordan  3h
  (load + filter by strategy + assemble context)
Write forge/runtime/team_runtime.py           Jordan  2h
  (instantiate agents from TeamConfig)
Write forge/runtime/task_router.py            Alex    2h
  (IC level routing + file ownership)
Write forge/skills/gap_detector.py            Sam     2h
Write scripts/validate_config.py              Alex    1h
Write scripts/validate_skills.py              Alex    1h
Write tests/unit/test_skill_engine.py         Sam     2h
Write tests/unit/test_config_loader.py        Sam     1h
Write tests/unit/test_task_router.py          Sam     1h
IC5 review + IC6 sign-off                     Morgan  3h
```

### Definition of Done
- [ ] `make validate-config` passes with zero errors on all YAML files
- [ ] `make validate-skills` passes with zero errors on all 8 skills
- [ ] `TeamRuntime` loads `team.yaml` → 5 agents with correct levels, skills, budgets
- [ ] `SkillEngine` loads `build_react_component` for IC4 → strategy=summary applied
- [ ] `SkillEngine` loads same skill for IC3 → strategy=full applied
- [ ] `SkillGapDetector` detects `implement_authentication` (not in IC3 skill set)
- [ ] `TaskRouter` routes auth task to IC5, simple component task to IC4
- [ ] Unit tests for all three components pass with >80% coverage

### Go / No-Go Criteria
> **GO if:** Config loads, skills load, routing works, tests pass.
> **NO-GO if:** Config validation is permissive (accepts bad YAML), skill loading errors silently.

---

## MILESTONE 3 — Gateway + Commander
**Duration:** Days 6–8 | **Owner:** Alex (IC4) + Sam (IC3)

### Goal
A human types `/forge ship "add contact form"` in Slack.
FORGE parses intent, creates a work order in Postgres, posts a confirmation back
to the thread. The conversation is bound to the run. Thread context is preserved.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write forge/gateway/slack_bot.py              Alex    3h
  (Slack Bolt app, socket mode, /forge handler)
Write forge/gateway/command_parser.py         Alex    2h
  (LLM intent parsing → structured work order fields)
Write forge/gateway/session_manager.py        Alex    2h
  (thread_ts ↔ run binding, project detection)
Write forge/agents/base.py                    Jordan  3h
  (skill context loading, tool dispatch, Claude call loop)
Write forge/agents/commander.py               Jordan  2h
  (intent → work order → Slack confirmation)
Write forge/workflow/state_machine.py         Jordan  2h
  (all states, all transitions, audit log on transition)
Write forge/workflow/orchestrator.py          Jordan  2h
  (phase dispatch to Celery)
Write forge/workflow/approval_gate.py         Alex    2h
  (post to Slack, emoji reaction handler, Postgres write)
Write forge/api/routes/slack.py               Alex    1h
  (Slack event webhook endpoint)
Write forge/api/routes/runs.py                Sam     1h
  (run status, audit log query endpoints)
Write alembic/versions/002_channels.py        Sam     1h
Write alembic/versions/003_work_orders.py     Sam     1h
Write tests/unit/test_state_machine.py        Sam     2h
Write tests/unit/test_approval_gate.py        Sam     2h
IC5 review + IC6 sign-off                     Morgan  2h
```

### Definition of Done
- [ ] `/forge ship "add newsletter signup"` → work order in DB within 3 seconds
- [ ] Slack thread shows: "Work Order #1 created — Starting planning..."
- [ ] Work order has: title, description, constraints, channel binding
- [ ] State transitions INTAKE → RESEARCHING logged to audit_log
- [ ] Emoji ✅ on approval message → approval record written to Postgres
- [ ] Emoji ❌ on approval message → rejection recorded, IC6 notified
- [ ] Invalid state transition (e.g. INTAKE → SHIPPED) → exception, not silent fail
- [ ] Unit tests for state machine cover all valid + 5 invalid transitions

### Go / No-Go Criteria
> **GO if:** End-to-end Slack → Postgres → Slack reply works reliably.
> **NO-GO if:** Thread binding fails, state machine allows invalid transitions, approval not persisted.

---

## MILESTONE 4 — Planning Loop
**Duration:** Days 9–10 | **Owner:** Jordan (IC5)

### Goal
After work order creation, the IC5 Planner agent activates, loads relevant skills
and memory, produces a task plan with IC levels assigned, posts a readable summary
to the Slack thread, and waits for human approval.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write forge/agents/planner.py                 Jordan  3h
  (decompose_epic_to_tasks skill, task graph output)
Write forge/memory/assembler.py               Jordan  3h
  (context assembly: facts + decisions + skills + run state)
Write forge/memory/writer.py                  Jordan  2h
  (write facts, decisions, artifacts with dedup)
Write forge/memory/retriever.py               Sam     2h
  (SQL-based retrieval, no pgvector yet)
Write forge/memory/embeddings.py              Sam     1h
  (stub — returns zeros until pgvector enabled)
Write alembic/versions/004_memory_tables.py   Sam     2h
Write forge/skills/gap_detector.py           (done M2)
Write forge/workflow/state_machine.py         (done M3)
  Add: PLANNING → AWAITING_PLAN_APPROVAL
Write Slack approval message formatter        Alex    1h
  (plan summary, task list, approve/reject prompt)
Write tests/unit/test_memory_assembler.py     Sam     2h
Write tests/unit/test_planner_output.py       Sam     1h
IC5 review + IC6 sign-off                     Morgan  2h
```

### Definition of Done
- [ ] Planner agent activates after work order creation (Celery task dispatched)
- [ ] Context assembled: project facts + standing decisions + relevant skills loaded
- [ ] Task plan contains: title, description, agent_role, dependencies, complexity, files_likely_touched
- [ ] Skill gap detection runs before plan posted — blocks if critical gap found
- [ ] Slack posts formatted plan summary with task list and ✅/❌ prompt
- [ ] Human ✅ → state transitions to EXECUTING, tasks written to DB
- [ ] Human ❌ → state returns to PLANNING with rejection note
- [ ] Plan approval recorded in `approvals` table with context_snapshot
- [ ] Memory writer deduplicates: same fact twice → updates, not duplicates

### Go / No-Go Criteria
> **GO if:** Full INTAKE → AWAITING_PLAN_APPROVAL → EXECUTING flow works end-to-end.
> **NO-GO if:** Planner generates tasks without skill assignment, approval not atomic.

---

## MILESTONE 5 — Execution Loop
**Duration:** Days 11–14 | **Owner:** Jordan (IC5) + Sam (IC3)

### Goal
Tasks execute in the correct order, assigned to the correct IC-level agent with
skills loaded at the right depth. Each task checkpoints after completion. IC3 can
escalate to IC5 mid-task. IC5 reviews all IC3/IC4 output before QA.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write forge/agents/builder.py                 Jordan  4h
  (IC3/IC4/IC5 skill-loaded, tool dispatch, constraint check)
Write forge/tools/file_ops.py                 Sam     2h
  (read_file, write_file, list_files with scope check)
Write forge/tools/git_ops.py                  Riley   3h
  (branch, commit, diff, PR creation)
Write forge/guardrails/scope_guard.py         Jordan  2h
  (pre-write constraint enforcement)
Write forge/guardrails/rate_limiter.py        Sam     1h
  (token counter per run in Redis)
Write forge/workflow/escalation.py            Jordan  2h
  (mid-task IC3→IC5 escalation, pause/resume task)
Write forge/workflow/handoff.py               Jordan  1h
  (structured context between agents)
Write forge/workflow/interrupt_handler.py     Riley   2h
  (P0/P1 preemption: checkpoint + pause)
Write forge/runtime/task_router.py            (done M2)
  Add: WIP limit enforcement, reassign on failure
Write forge/agents/reviewer.py                Jordan  2h
  (IC5 reviews IC3/IC4 output against skill quality criteria)
Write alembic/versions/005_tasks_handoffs.py  Sam     1h
Write alembic/versions/006_escalations.py     Sam     1h
Write tests/unit/test_builder_constraints.py  Sam     2h
Write tests/unit/test_escalation.py           Sam     2h
Write tests/integration/test_execution_loop.py Sam    3h
IC5 review + IC6 sign-off                     Morgan  3h
```

### Definition of Done
- [ ] IC4 Builder executes a task, writes a file, commits — checkpoint saved
- [ ] IC3 Builder loads full skill procedure; IC5 loads principles only
- [ ] Scope guard blocks IC3 writing to `src/auth/**` → BLOCKED state, IC6 notified
- [ ] Rate limiter alerts at 80% token budget, hard stops at 100%
- [ ] IC3 can call `flag_blocked` → escalation created → IC5 answers → task resumes
- [ ] IC3 fails 3× → task auto-reassigned to IC4 → IC6 notified in Slack
- [ ] IC5 Reviewer produces review_report with structured change requests
- [ ] Review comments that need fixes → task back to EXECUTING with context
- [ ] WIP limit enforced: 4th concurrent task queued, not dispatched
- [ ] P0 interrupt → active run checkpointed + paused, incident run created
- [ ] All tasks in a run complete → state transitions to VERIFYING

### Go / No-Go Criteria
> **GO if:** Full execution loop runs on a real Next.js file. Constraints enforced. Escalation works.
> **NO-GO if:** Agents write files without constraint check, WIP unlimited, escalation aborts run.

---

## MILESTONE 6 — Quality + Ship
**Duration:** Days 15–17 | **Owner:** Riley (IC4) + Alex (IC4)

### Goal
After execution, QA pipeline runs all checks. Security scan passes. IC6 sees a
structured ship approval request with full context. Approves → PR merged → deploy
verified → SHIPPED. Release notes generated and posted.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write forge/tools/test_runner.py              Riley   2h
  (run pytest, parse output, structured test report)
Write forge/tools/static_analysis.py         Riley   1h
  (ruff check, TypeScript compile if applicable)
Write forge/tools/secrets_scan.py            Riley   1h
  (trufflehog or detect-secrets integration)
Write forge/guardrails/security_pipeline.py  Riley   2h
  (orchestrate: secrets + sast + dep audit)
Write forge/agents/qa.py                     Jordan  2h
  (QA agent: run checks, triage failures, produce report)
Write forge/agents/security.py               Jordan  2h
  (Security agent: analyze diff, run scans, veto or clear)
Write forge/agents/release.py                Jordan  2h
  (write_release_notes, write_pr_description skills)
Write forge/tools/git_ops.py                 (done M5)
  Add: create_pr, merge_pr via GitHub API
Write forge/api/routes/slack.py              (done M3)
  Add: GitHub webhook handler for CI checks
Write forge/workflow/deploy_verifier.py      Riley   2h
  (wait for deploy webhook, smoke test, error rate check)
Write ship approval Slack message formatter  Alex    1h
  (diff summary, test report, lighthouse, security verdict)
Write /forge status command handler          Alex    2h
  (ProjectStatusAssembler → formatted Slack reply)
Write tests/unit/test_qa_pipeline.py         Sam     2h
Write tests/integration/test_full_ship.py    Sam     3h
IC5 review + IC6 sign-off                    Morgan  3h
```

### Definition of Done
- [ ] QA pipeline runs: lint, TypeScript compile, tests, security scan in sequence
- [ ] Any critical security finding → automatic BLOCKED, IC6 must override
- [ ] Ship approval posted to Slack with: diff summary, test pass/fail, security verdict
- [ ] IC6 reacts ✅ → PR merged via GitHub API → branch deleted
- [ ] IC6 reacts ❌ with note → run returns to EXECUTING with rejection context
- [ ] CI checks failure (from GitHub webhook) → run transitions back to EXECUTING
- [ ] Deploy verification waits for Vercel webhook, then runs smoke tests
- [ ] Release notes and PR description generated, posted to thread
- [ ] `/forge status` returns: active runs, blocked items, recent ships, team WIP
- [ ] Full INTAKE → SHIPPED run completes in Slack with zero manual interventions
  (other than the two required human approval gates)

### Go / No-Go Criteria
> **GO if:** One complete run from /forge ship → SHIPPED works on a real Next.js repo.
> **NO-GO if:** Security scan skippable, ship approval not recorded in DB, CI failures ignored.

---

## MILESTONE 7 — End-to-End + Hardening
**Duration:** Days 18–20 | **Owner:** All engineers

### Goal
Run FORGE on three real features in sequence. Identify and fix all edge cases.
Resumption works after simulated crash. Memory conflict detection alerts correctly.
Blocked items escalate automatically after 4 hours.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Run Feature 1: "Add contact form"             Jordan  live run
  → identify all runtime gaps, fix immediately
Run Feature 2: "Add newsletter signup"        Jordan  live run
  → validate memory reuse from Feature 1
Run Feature 3: "Add dark mode toggle"         Jordan  live run
  → validate skill learning after 2 prior runs
Simulate crash mid-run → verify resumption    Riley   3h
Simulate blocked run > 4h → verify escalation Sam     1h
Test memory conflict: write contradicting     Sam     2h
  fact → verify IC6 notified before promote
Test task reassignment: force 3× IC3 fail     Sam     2h
  → verify auto-escalate to IC4
Load test: 3 concurrent runs                  Riley   2h
  → verify WIP limit enforced
Test interrupt: P0 during active run          Riley   2h
  → verify checkpoint + pause + incident run
Write scripts/replay_run.py                   Sam     2h
  (debug: replay any run from audit log)
Write scripts/memory_audit.py                 Sam     1h
  (print memory state for a project)
Write forge/observability/metrics.py          Riley   2h
  (token spend per run, task throughput, phase latency)
Fix all issues found in live runs             All     varies
IC6 final architecture review                 Morgan  3h
```

### Definition of Done
- [ ] Three complete features shipped on real repo with zero manual code edits
- [ ] Crash simulation: kill forge-worker mid-task → restart → run resumes correctly
- [ ] Memory from Feature 1 used in Feature 2 context (verified via assembler log)
- [ ] Skill confidence updated after each feature run (verified in DB)
- [ ] 3 concurrent runs: each gets max 1-2 tasks dispatched (WIP enforced)
- [ ] `/forge status` reflects real-time state across all 3 runs
- [ ] Audit log has complete trail for all 3 features: every transition, approval, tool call
- [ ] IC6 code review complete on all components

### Go / No-Go Criteria
> **GO if:** Three real features shipped. System resumable. Memory reused. Audit complete.
> **NO-GO if:** Any run requires manual intervention beyond the two approval gates.

---

## MILESTONE 8 — Production Deploy
**Duration:** Days 21–22 | **Owner:** Riley (IC4) + Morgan (IC6)

### Goal
FORGE running in production (cloud). CI/CD pipeline deploys on merge to main.
Monitoring dashboards live. On-call runbook written.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Provision cloud infra (AWS/GCP/Fly.io)       Riley   3h
  Postgres RDS, Redis ElastiCache or managed
Configure docker-compose.prod.yml             Riley   2h
Configure nginx + SSL (Let's Encrypt)         Riley   1h
Write .github/workflows/deploy.yml            Riley   2h
  (test → lint → build → push → deploy on merge to main)
Configure secrets in GitHub Actions           Riley   1h
  (no secrets in code, env, or .env.prod in git)
Configure Datadog or Grafana Cloud            Riley   2h
  (metrics: token spend, run throughput, error rate)
Configure Sentry for error tracking           Riley   1h
Write runbook: on-call procedures             Riley   2h
  (how to debug a stuck run, how to manually resume)
Smoke test production environment             All     2h
IC6 production sign-off                       Morgan  2h
```

### Definition of Done
- [ ] `git push origin main` → CI runs tests → deploys to production automatically
- [ ] `/forge ship` in Slack hits production FORGE, not local
- [ ] Postgres in production has all migrations applied
- [ ] Redis in production has correct maxmemory and persistence
- [ ] Sentry capturing errors from all services
- [ ] Grafana/Datadog showing: active runs, token spend/hour, error rate, queue depth
- [ ] IC6 has reviewed and approved production configuration
- [ ] Runbook covers: how to restart a stuck run, how to rollback FORGE itself

### Go / No-Go Criteria
> **GO if:** Production environment identical to dev. CI/CD green. Monitoring live.
> **NO-GO if:** Secrets in code, missing monitoring, no rollback procedure for FORGE itself.

---

## ★ MVP COMPLETE — End of Week 4

At this point, FORGE is:
- Running in production
- Accepting `/forge ship` commands from Slack
- Executing 5-agent IC-level workflows with skill-loaded execution
- Enforcing all guardrails (scope guards, approval gates, security scans)
- Persisting full memory and audit trail
- Resumable after crash or interruption
- Observable in production

---

## MILESTONE 9 — Memory + Learning (Phase 2)
**Duration:** Days 23–27 | **Owner:** Jordan (IC5) + Sam (IC3)

### Goal
pgvector semantic search live. Skill learning loop running.
Post-run summarizer promotes run memory to project memory.
KnowledgeIngestionAgent watching at least 2 external feeds.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Enable pgvector: 002 migration update         Sam     1h
  Add vector(1536) columns + ivfflat indexes
Write forge/memory/embeddings.py              Jordan  2h
  (real Anthropic embeddings, was stub in MVP)
Update forge/memory/retriever.py              Jordan  2h
  (semantic search via pgvector cosine similarity)
Write forge/memory/summarizer.py              Jordan  3h
  (post-run: condense run memory → project facts)
Write forge/skills/learning.py                Jordan  3h
  (outcome scoring, confidence update, EMA)
Write forge/skills/promoter.py                Jordan  2h
  (pattern extraction → skill_promotions table)
Write forge/skills/ingestion/feed_processor.py Sam    3h
  (KnowledgeIngestionAgent: reads feeds, proposes patches)
Configure 2 feeds: nextjs_changelog           Sam     1h
                   npm_security_advisories
Write forge/skills/drills/drill_runner.py     Sam     3h
Write 3 skill drill fixtures                  Sam     2h
  (build_react_component, design_rest_api, write_unit_test)
Write forge/skills/staleness.py               Sam     2h
Write Celery beat schedules:                  Riley   2h
  - summarize_run (after every run)
  - decay_memory_relevance (weekly)
  - check_blocked_runs (every 30 min)
  - check_skill_feeds (daily)
Enable /forge memory search command           Alex    2h
Write tests/integration/test_learning_loop.py Sam    2h
IC5 review + IC6 sign-off                     Morgan  2h
```

### Definition of Done
- [ ] pgvector semantic search returns more relevant facts than SQL keyword search
  (verified on 3 queries against populated project memory)
- [ ] Post-run summarizer produces ≤20 project-level facts from a run's scratchpad
- [ ] Skill confidence updated correctly after a run with IC6 review corrections
- [ ] `nextjs_changelog` feed processed → at least 1 `SkillPatchProposal` created
- [ ] `npm audit` advisory → auto-applied to skill `known_gotchas` (critical severity)
- [ ] Skill drill for `build_react_component` runs, scores IC4 agent
- [ ] `/forge memory search auth patterns` → relevant facts returned in Slack
- [ ] `check_blocked_runs` beat job fires, finds BLOCKED run > 4h, notifies IC6

### Go / No-Go Criteria
> **GO if:** Memory improves with each run. Feed ingestion works. Drills score correctly.
> **NO-GO if:** Embeddings timeout, summarizer produces noise, drills always pass regardless of output quality.

---

## MILESTONE 10 — Reporting + Maintenance (Phase 2)
**Duration:** Days 28–30 | **Owner:** Alex (IC4) + Riley (IC4)

### Goal
Daily digest posted automatically. Backlog management via Slack commands.
Dependency update workflow creates work orders automatically.
Technical debt registry populated from code reviews.

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write forge/reporting/digest.py               Alex    3h
  (daily summary: what shipped, in progress, blocked, coming)
Configure Celery beat: daily digest 9am UTC   Riley   30m
Write /forge backlog command                  Alex    2h
  (list, add, prioritize work orders)
Write /forge blocked command                  Alex    1h
  (list all blocked items across team)
Write forge/maintenance/dep_updater.py        Sam     3h
  (detect outdated deps → create work order → IC4 updates)
Write forge/maintenance/debt_registry.py      Sam     2h
  (extract debt items from review comments → debt_items table)
Write alembic/versions/010_phase2_tables.py   Sam     2h
  (backlog, sprints stub, debt_items, digests)
Write forge/maintenance/flag_cleanup.py       Sam     1h
  (detect feature flags older than N days → create task)
Configure performance regression check        Riley   2h
  (Lighthouse on main branch weekly, alert on regression)
Write IC6 weekly summary report               Alex    2h
  (velocity, token spend, skill confidence trends, debt)
IC5 review + IC6 sign-off                     Morgan  2h
```

### Definition of Done
- [ ] Daily digest posted to team Slack channel at 9am UTC every day
- [ ] `/forge backlog` lists all pending work orders sorted by priority
- [ ] Dependency updater creates work order for outdated dep (tested with a pinned-old dep)
- [ ] Debt item extracted from a code review with ≥3 change requests
- [ ] Feature flag older than 30 days → cleanup task auto-created
- [ ] IC6 weekly report generated with velocity + spend + skill confidence

---

## Phase 3 Backlog (Weeks 7–12)

```
INITIATIVE                    DESCRIPTION                          OWNER (TBD)
──────────────────────────────────────────────────────────────────────────────
Sprint / iteration model      Sprints table, sprint planning       Jordan
                              ceremony, velocity tracking
Epic grouping                 Epics above work orders,             Jordan
                              roadmap-level planning
Capacity planning             Team load vs available WIP,          Jordan
                              realistic sprint commitment
Staged / canary rollout       Gradual deploy with auto-rollback    Riley
Cross-work-order deps         Dependency enforcement at planning   Jordan
Stakeholder report            Non-technical summary generation     Alex
Multi-repo support            Feature spanning two repos           Jordan
Planned vs actual tracking    Planner estimates vs reality,        Sam
                              feed back to decompose skill
Retro workflow                After every N runs, trigger retro,  Alex
                              track action items
Project health dashboard      Web UI: metrics, trends, costs       Alex
Environment drift detection   Dev/staging/prod config comparison   Riley
Enterprise: LDAP/SSO          Role-based access from org directory Morgan
Enterprise: chargeback        Token cost per team/cost center      Sam
Enterprise: compliance export SOX/HIPAA audit report from log     Sam
```

---

## Daily Rhythm

```
EACH DAY:
  09:00  Async standup in Slack (3 sentences per engineer — no meeting)
         Format: "Shipped: X | In progress: Y | Blocked: Z"

  Continuous:  PRs go to IC5 Jordan for review first
               IC5 approves → goes to IC6 Morgan for final review
               IC6 approves → merge

  End of day:  Each engineer updates their task status in Slack thread
               Any blockers escalated explicitly to Morgan

EACH WEEK:
  Monday:   Milestone check — are we on track? Adjust if not.
  Friday:   Demo: show what shipped this week on real running FORGE
            No slides. Live demo on the actual system.
            IC6 Morgan gives written feedback in Slack thread.
```

---

## Risk Register

```
RISK                            PROB  IMPACT  MITIGATION
────────────────────────────────────────────────────────────────────────────
Anthropic API latency spikes    MED   HIGH    Retry with exponential backoff
                                              Circuit breaker → BLOCKED state
                                              Haiku for lightweight tasks

Celery job lost on crash        MED   HIGH    acks_late=True on all tasks
                                              Resume from Postgres on startup
                                              Heartbeat monitoring in Redis

pgvector slow on large memory   LOW   MED     SQL fallback flag (no vectors)
                                              Partition memory tables by month
                                              IVFFlat index tuning

Scope too large for 2-week MVP  HIGH  HIGH    Strict: only M1-M6 is MVP
                                              M7-M8 is buffer, not scope creep
                                              IC6 cuts scope, not quality

IC3 produces poor output        MED   MED     IC5 reviews all IC3 output
                                              Task reassignment on 3× failure
                                              Skill drills calibrate IC3 skills

Slack socket mode drops         LOW   MED     Reconnect handler in bolt app
                                              Idempotent message posting
                                              Dead letter queue in Redis

Secret in agent output          LOW   CRITICAL Secrets scan on every artifact
                                              before storage
                                              Security agent veto on ship

Over-engineering slows team     MED   HIGH    IC6 enforces: build the simplest
                                              thing that passes the milestone DoD
                                              No gold-plating permitted
```

---

## Definition of MVP Complete

FORGE v1.0 is shipped when ALL of the following are true:

```
✅ A human types /forge ship in Slack
✅ FORGE creates a work order with constraints
✅ IC5 Planner decomposes into tasks with IC levels and skills assigned
✅ Human approves plan (Approval Gate 1)
✅ Builders execute tasks with skills loaded at correct depth
✅ IC5 reviews IC3/IC4 output before QA
✅ QA pipeline runs: lint, tests, security scan
✅ IC6 approves ship (Approval Gate 2)
✅ PR merged, deploy verified, release notes generated
✅ Full audit trail in Postgres for every decision
✅ Memory reused on the next run for the same project
✅ Skill confidence updated after each run
✅ System resumes correctly after simulated crash
✅ All guardrails enforced at infrastructure level (not just prompts)
✅ Running in production with CI/CD and monitoring
```
