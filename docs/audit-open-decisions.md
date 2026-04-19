# Phalanx — Architecture Audit: Open Decisions

**Audit date:** 2026-04-18
**Repo state at audit:** `github.com/usephalanx/phalanx` @ commit `02d123b` (main)
**Audited by:** Raj + Claude (principal-engineer review)
**Method:** Read-only audit across `phalanx/`, `alembic/`, `docker-compose*.yml`, `Dockerfile`, `.github/workflows/`, `docs/`, `tests/`, plus sibling repos `~/usephalanx-website` and `~/usephalanx-infra`.

This document captures **11 open decisions** surfaced by the audit. Each item is anchored to file/line evidence. No code changes have been made. Items will be resolved one at a time in collaboration between Raj (CTO/founder) and the principal engineer.

Operating rules in force:
- No deploys without explicit approval.
- No redesigns without discussion.
- Never introduce dual pipelines.
- Data-anchored decisions only.

---

## Status legend
- **OPEN** — not yet discussed
- **NEEDS CLARITY** — partially discussed, more info needed
- **RESEARCHING** — research approved, proposal pending
- **APPROVED** — direction agreed, implementation pending
- **DECIDED** — policy set, implementation pending

---

## A. "Forge" → "Phalanx" rebrand is incomplete

- **Status:** OPEN
- **Severity:** HIGH (crosses env vars, Slack App manifest, Redis keys, Docker volumes, git-bot identity)
- **Evidence (11 files, 22+ occurrences):**
  - Env vars: `FORGE_ENV`, `FORGE_API_KEY`, `FORGE_MAX_TOKENS_PER_RUN`, `FORGE_S3_BUCKET`, `FORGE_CORS_ORIGINS`, `FORGE_STREAMING_BUILDER`, +7 more → [phalanx/config/settings.py](../phalanx/config/settings.py)
  - Celery app name: `Celery("forge", ...)` → [phalanx/queue/celery_app.py:14](../phalanx/queue/celery_app.py#L14) — changing this changes Redis task routing keys; in-flight tasks would get stranded on cutover.
  - Slack command: `/forge build` → [phalanx/gateway/command_parser.py:2](../phalanx/gateway/command_parser.py#L2) — requires Slack App manifest change.
  - Health response: `service="forge-api"` → [phalanx/api/routes/health.py:126](../phalanx/api/routes/health.py#L126)
  - Git bot: `git_author_email: "forge-bot@acme.com"` → [phalanx/config/settings.py:81](../phalanx/config/settings.py#L81) — changing this splits PR/commit attribution across two identities.
  - Docker volumes (data-bearing): `forge-postgres-data`, `forge-redis-data`, `forge-repos`, `forge-pip-cache`, `forge-pgadmin-data` → [docker-compose.yml](../docker-compose.yml), [docker-compose.prod.yml](../docker-compose.prod.yml) — renaming drops data.
  - Networks: `forge-net` (both compose files).
  - `pyproject.toml:8` description: `"FORGE — Configurable AI Team Operating System"`.
  - README.md line 59: says `/tmp/forge-repos/`; code uses `/tmp/phalanx-repos/`.
  - CHANGELOG.md line 41: references `/forge build` and `FORGE_WORKER=1` (current: `PHALANX_WORKER=1`).
  - Dockerfile line 74: `groupadd --gid 1001 forge`.

- **Why it matters:** A half-done rename creates a dual-identity system — the exact "dual pipeline" class of problem we said to avoid. Running configs/data under "forge" names while marketing/docs say "Phalanx" creates drift that compounds.

- **Proposed directions:**
  - **Option 1 — Coordinated cutover (preferred):** Single release that renames env vars + Slack App + Celery app + Docker volumes + git-bot identity in lockstep. Requires brief downtime (Celery queue drain + volume rename via `docker volume create` + data copy). Infra repo `.env.prod` must update same window.
  - **Option 2 — Shimmed stage:** Introduce `PHALANX_*` env vars that shadow `FORGE_*` with fallback; rename everything else; leave env vars for a later cutover. Lower downtime risk, but accepts 2-6 weeks of duality.
  - **Option 3 — Defer indefinitely:** Accept cosmetic drift, freeze "forge" as internal name, ensure no new `forge_*` identifiers.

- **Decision:** PENDING

---

## B. pgvector extension is assumed but never created

- **Status:** RESOLVED (audit error — already in code)
- **Correction 2026-04-18:** My original audit missed this. The initial migration [alembic/versions/20260317_0001_initial_schema.py:22-25](../alembic/versions/20260317_0001_initial_schema.py#L22-L25) already runs `CREATE EXTENSION IF NOT EXISTS "vector"` (plus `"uuid-ossp"` and `"pg_trgm"`) as the first op of `upgrade()`. Fresh Postgres instances are covered. No action needed.
- **What I got wrong:** I claimed "no `CREATE EXTENSION` anywhere in `alembic/versions/` (verified by search)" — that verification was wrong. The statement is present.
- **Lesson for future audits:** re-verify negative claims with a second grep pass before flagging HIGH severity.

---

## D. OpenTelemetry installed but never initialized

- **Status:** OPEN
- **Severity:** MEDIUM (observability debt; dead dependency carrying attack surface)
- **Evidence:**
  - Deps: `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-sqlalchemy`, `opentelemetry-exporter-otlp` → [pyproject.toml:52-55](../pyproject.toml#L52-L55)
  - No `TracerProvider`, no exporter wiring, no span emission found anywhere in the codebase.
  - Only structlog → stdout → Docker `json-file` logs (lost on container restart without mounted volume).

- **Why it matters:** A multi-agent pipeline with retries, approvals, and sandboxes is opaque without distributed tracing. Agents' decisions, token spend, and gate outcomes are not correlatable across services.

- **Proposed directions:**
  - **Option 1 — Wire OTel to a backend:** Need choice — Honeycomb, Grafana Tempo, Jaeger (self-hosted), Datadog, Axiom. Adds `TracerProvider` setup in `phalanx/observability/`, FastAPI + SQLAlchemy + Celery instrumentation, OTLP endpoint in `.env.prod`.
  - **Option 2 — Remove the deps:** If no tracing backend is planned near-term, strip the 4 packages from `pyproject.toml`. Reduces image size and removes un-audited attack surface.
  - **Option 3 — Minimal viable:** Start with Grafana Tempo self-hosted on the same Lightsail host (or a small sibling). Keeps costs near zero, gives traces without external vendor commitment.

- **Decision:** PENDING — choice of backend blocks progress.

---

## E. CI deploy job is a stub; deploys are laptop-driven

- **Status:** OPEN
- **Severity:** MEDIUM (no GitOps audit trail, no automated rollback, key-person risk)
- **Evidence:**
  - [.github/workflows/ci.yml](../.github/workflows/ci.yml) — builds images and pushes to `ghcr.io`, but the Deploy job near the bottom is a placeholder (no `deploy.sh` invocation).
  - Real deploys happen from Raj's laptop via `./deploy.local.sh` in `~/usephalanx-infra` → wraps `./deploy.sh` in the main repo.
  - `deploy.sh` health-check failure only logs a warning (line 161-169) — no automatic rollback to prior tag.

- **Why it matters:** No deploy audit trail (who deployed, when, what tag). No rollback path. Only Raj can deploy. Scales poorly.

- **Proposed directions:**
  - **Option 1 — CI-gated manual-approval deploy:** Move `deploy.sh` into a GH Actions job with `environment: production` (manual gate). Self-hosted runner on Lightsail host OR SSH-from-cloud-runner with a deploy-only SSH key stored in GH Secrets.
  - **Option 2 — Keep laptop-driven:** Intentional for now (single operator, high-trust). Add logging to `deploy.local.sh` for per-deploy records.
  - **Option 3 — Argo CD / Flux (overkill for Lightsail):** Dismissed — GitOps-on-Kubernetes pattern not matched to current single-host topology.

- **Decision:** PENDING — Option 1 recommended.

---

## F. Soul layer defined on `BaseAgent` but not wired into CI Fixer (and no agent scoping)

- **Status:** OPEN
- **Severity:** MEDIUM
- **Evidence:**
  - Soul methods defined: `_load_cross_run_memory`, `_write_cross_run_pattern`, `_load_complexity_calibration`, `_write_complexity_calibration` → [phalanx/agents/base.py:548-700](../phalanx/agents/base.py#L548-L700)
  - Grep for callers inside [phalanx/agents/ci_fixer.py](../phalanx/agents/ci_fixer.py) and [phalanx/ci_fixer/](../phalanx/ci_fixer/) → zero matches.
  - Commit `473528c` advertised "soul layer, CI fixer agent, …" — the CI Fixer code path does not call the soul methods.
  - `MemoryFact` has no `agent_role` column → [phalanx/db/models.py:470-510](../phalanx/db/models.py#L470) — when multiple agents share memory, CI Fixer reads engineering facts and vice versa (cross-contamination).

- **Why it matters:** Feature advertised without being live. Worse, the shared-memory design is unscoped — if we *do* wire CI Fixer to the soul layer today, it sees builder/reviewer facts that are irrelevant and potentially misleading for CI triage.

- **Proposed directions:**
  - **Option 1 — Narrow soul layer to Builder/Commander:** Document scope; explicitly exclude CI Fixer.
  - **Option 2 — Wire CI Fixer + add `agent_role` scope:** Migration to add `MemoryFact.agent_role` column (nullable for legacy, required for new writes). CI Fixer writes/reads only its own scope.
  - **Option 3 — Two memory substrates:** Separate table `ci_fixer_memory` for CI Fixer learnings; keep `MemoryFact` for engineering agents. More isolation, more code.

- **Decision:** PENDING — will be informed by upcoming CI Fixer brainstorm.

---

## H. Abandoned / unreferenced directories at repo root

- **Status:** OPEN
- **Severity:** LOW (clutter; confusion for new contributors)
- **Evidence:**
  - [kanban-board/](../kanban-board/) — full-stack demo app. Not referenced in any compose, CI, or deploy.
  - [wordpress-site/](../wordpress-site/) — WordPress docker-compose config. Not referenced anywhere.
  - [salon-booking/](../salon-booking/) — `backend/pyproject.toml`. Not referenced.
  - [site-next/](../site-next/) — Next.js marketing site with 50K-line PLAN.md, SETUP.md. Not built in CI, not deployed, not mounted in nginx. Unclear if planned replacement for `site/` or archived.

- **Why it matters:** Looks like Phalanx-generated demo apps committed back into the mothership repo. Creates confusion about which code is product vs. generated artifact.

- **Proposed directions:**
  - **Option 1 — Delete all four:** Keep repo lean; generated demos belong in a separate showcase repo (mentioned in README as `github.com/usephalanx/showcase`).
  - **Option 2 — Move to `examples/` or `samples/`:** If they serve as reference material for agents, isolate them.
  - **Option 3 — Promote `site-next/` and delete the rest:** If Next.js is the intended marketing site, wire it into deploy and retire `site/`.

- **Decision:** PENDING — need confirmation on `site-next/` intent.

---

## I + J. DAG mode has triple-encoded dependencies and silently soft-fails quality gates

- **Status:** OPEN
- **Severity:** MEDIUM (blocks safe enablement of DAG flag)
- **Evidence:**
  - Three encodings of task dependencies: `Task.parent_task_id` (self-ref FK), `Task.depends_on` (ARRAY of task ids), and standalone `TaskDependency` table → [phalanx/db/models.py:193-261](../phalanx/db/models.py#L193-L261). No invariant enforced that they stay in sync.
  - DAG orchestrator silently marks qa/reviewer/verifier/integration_wiring failures as COMPLETED so the DAG doesn't stall → [phalanx/workflow/orchestrator.py:180-210](../phalanx/workflow/orchestrator.py#L180-L210). Builder failures halt; others are optimistic.
  - Feature flag: `phalanx_enable_dag_orchestration` gates the entire path.

- **Why it matters:** Flipping the flag today turns approval/quality gates into advisory (QA can fail and run still ships). This directly contradicts the README's "approval at every gate" promise. And the triple-encoded graph means any DAG writer has to touch three places correctly.

- **Proposed directions:**
  - **Option 1 — Consolidate to one representation:** Drop `TaskDependency` table OR drop `Task.depends_on` array OR drop `parent_task_id`. Industry pattern: dedicated edge table (`TaskDependency`) with FK constraints is cleanest for graphs.
  - **Option 2 — Fix soft-fail:** DAG mode must halt on reviewer/qa/verifier failure unless a `soft_fail: true` flag is explicit on the task. No silent COMPLETED.
  - **Option 3 — Defer DAG entirely:** Keep flag off; remove the DAG code path until consolidation is done.

- **Decision:** PENDING — I+J are interdependent; should be resolved together.

---

## N1. Docker socket now mounted on **general** worker — privilege broadened

- **Status:** APPROVED (split into dedicated CI Fixer worker)
- **Severity:** HIGH (prompt-injection or code-gen bug in any agent → host root)
- **Evidence:**
  - [docker-compose.prod.yml:113](../docker-compose.prod.yml#L113) — `phalanx-worker` runs queues `default,commander,planner,reviewer,qa,release,security,builder,ingestion,skill_drills,ci_fixer`.
  - Commit `4225f73` added [docker-compose.prod.yml:116](../docker-compose.prod.yml#L116) `user: root` and [:124](../docker-compose.prod.yml#L124) `/var/run/docker.sock` mount to that same service.
  - Previously, Docker socket was scoped only to `phalanx-sre-worker` → [docker-compose.prod.yml:145](../docker-compose.prod.yml#L145) — deliberate least-privilege split.

- **Why it matters:** Docker socket access is root-equivalent on the host. Every agent running on `phalanx-worker` (Commander, Planner, Builder, Reviewer, QA, Security, Release) now has that privilege, even though only CI Fixer needs it.

- **Approved direction:** Split CI Fixer onto its own Celery worker with the socket mount. Mirrors the existing `phalanx-sre-worker` pattern:
  - New service `phalanx-ci-fixer-worker` in [docker-compose.prod.yml](../docker-compose.prod.yml): `--queues=ci_fixer`, `user: root`, socket mount, own resource limits.
  - Remove `ci_fixer` from `phalanx-worker`'s queue list.
  - Remove `user: root` and socket mount from `phalanx-worker`.
  - Docker CLI can stay in the base image (used by both ci_fixer and sre workers).

- **Implementation notes:** ~20 lines of compose. No migration. Zero-downtime: deploy new service first (it takes over `ci_fixer` queue), then remove mount from general worker on the next deploy.

- **Decision:** APPROVED 2026-04-18 — implementation pending.

---

## N2. Sandbox isolation relaxed (root + bridge network) — research industry patterns

- **Status:** RESEARCHING (research green-lit)
- **Severity:** HIGH (wider container-escape surface + external egress for untrusted code)
- **Evidence:**
  - [phalanx/ci_fixer/sandbox_pool.py:334-336](../phalanx/ci_fixer/sandbox_pool.py#L334-L336) — commit `02d123b` removed `--network none`, removed `--user 1000:1000`, removed `--no-new-privileges`. Now: `--network bridge` as root.
  - Rationale in commit: `pip install` during GPT-driven env setup needs network + root.

- **Why it matters:** Running untrusted generated code inside a root container with external egress is the exact pattern CI runners avoid. Ephemeral lifetime mitigates data exfil risk but doesn't eliminate it; host kernel exploits are still reachable.

- **Research scope (approved):**
  - Pre-warmed images with deps baked in (dep-install happens at image-build time, not runtime).
  - Local pip proxy (devpi, JFrog, Artifactory, Nexus) — sandbox reaches only the proxy, not PyPI directly.
  - Rootless container runtimes: Podman rootless, Sysbox, Docker rootless mode.
  - Stronger isolation: Firecracker microVMs (AWS uses these for Lambda sandboxes), gVisor (Google App Engine), Kata Containers.
  - Nix-based reproducible sandboxes (declarative dep resolution).
  - Industry practice at: Buildkite Agent, CircleCI executors, GitHub Actions runners, Replit Nix sandboxes, Modal.com sandboxes, Fly Machines.

- **Proposal output:** Comparison matrix — isolation guarantee, dep-install UX, cold-start latency, operational cost, integration with existing Docker-based SRE pipeline.

- **Decision:** RESEARCH APPROVED 2026-04-18 — proposal to follow.

---

## N3. Dual validation path (sandbox OR local fallback) — sandbox-only decided

- **Status:** DECIDED (sandbox-only, fail loudly if sandbox can't provision)
- **Severity:** HIGH (correctness — local fallback validates against worker env, not project env)
- **Evidence:**
  - [phalanx/ci_fixer/validator.py:148-189](../phalanx/ci_fixer/validator.py#L148-L189) — `validate_fix()` branches on `sandbox_result.env_ready`. If sandbox ready → run in container. Else → run on host filesystem.
  - Silent fallback: GPT env setup returns empty on any error → [phalanx/ci_fixer/sandbox.py:380-385](../phalanx/ci_fixer/sandbox.py#L380-L385). That can leave `env_ready=True` with nothing actually installed.

- **Why it matters:** Two code paths, two sets of bugs, different correctness guarantees. Local path validates against whatever Python the worker has, which is not the project's env → false passes possible.

- **Decision (2026-04-18):** Everything runs in sandbox in prod. No local fallback.
  - Remove local validation path from [phalanx/ci_fixer/validator.py](../phalanx/ci_fixer/validator.py) entirely in prod mode.
  - If sandbox provisioning fails, the CI fix run fails with a clear error (not a silent fallback to local).
  - Add telemetry: `ci_fix_runs.validation_path` column (or AuditLog event) recording sandbox outcome.
  - Local path may be retained strictly behind a dev/test flag for offline iteration, but MUST NOT be reachable in prod.

- **Implementation notes:** Pair with N1 (dedicated worker) since ci-fixer worker alone needs the Docker socket. Depends on N2 research for which sandbox strategy to use long-term.

- **Decision:** DECIDED 2026-04-18 — implementation pending (blocked on N2 research).

---

## N4. Dual commit strategy (`author_branch` for lint-only, `fix_branch` otherwise)

- **Status:** NEEDS CLARITY
- **Severity:** MEDIUM (trust-boundary change — Phalanx writes to author branches)
- **Evidence:**
  - [phalanx/agents/ci_fixer.py:453-506](../phalanx/agents/ci_fixer.py#L453-L506) — if `parsed.lint_errors and not type/test/build errors` → commit directly to author's branch, no PR. Otherwise → `phalanx/ci-fix/{run_id}` branch + PR.
  - [docs/MULTI_AGENT_CI_FIXER.md](MULTI_AGENT_CI_FIXER.md) previously stated "Never open a second fix PR" and implied fixes land only on a Phalanx-owned branch.
  - Bot-loop guard: [phalanx/api/routes/ci_webhooks.py:313-327](../phalanx/api/routes/ci_webhooks.py#L313-L327) skips check_runs from `settings.git_author_name` (case-insensitive).
  - New DB columns to track outcome: `fix_strategy`, `fix_branch_ci_status` → [alembic/versions/20260418_0001_ci_fixer_closed_loop.py](../alembic/versions/20260418_0001_ci_fixer_closed_loop.py).

- **Why it matters:** Writing to the author's own branch is a product-level behavior change with UX wins (lint errors vanish without review friction) and trust-boundary risks (rebase/force-push collisions; unexpected Phalanx commits in author's history; branch-protection interaction).

- **Open questions for Raj:**
  1. Should `author_branch` strategy be opt-in per-repo via `CIIntegration` config (safer default) or system-wide (current)?
  2. How do we handle author force-pushes that overwrite a Phalanx lint commit? (Currently: we re-run and re-fix; but silent overwrite loses audit trail.)
  3. Should the Tier 1 commit be signed (GPG) so authors can filter their history?
  4. What is the desired UX in Slack — notify the author the moment lint fix lands, or stay silent on auto-fixes?
  5. Does the `fix_branch_ci_status` tracking ladder up to any human-visible dashboard, or is it internal only?

- **Decision:** NEEDS CLARITY 2026-04-18 — will resolve during the CI Fixer brainstorm.

---

## Also noted (tactical, not requiring a strategic decision)

### C. X-API-Key non-constant-time comparison
- [phalanx/api/main.py:70](../phalanx/api/main.py#L70) — `api_key != settings.forge_api_key`. Should use `hmac.compare_digest()`. One-line fix. Classic timing-attack surface.

### G. Production image carries nodejs/npm/pytest/ruff
- [Dockerfile:66-83](../Dockerfile#L66-L83) — dev tools in prod runtime. Justified today because QA agent shells out to pytest/ruff against generated code inside the worker container. Cleaner pattern (sandbox-only validation per N3) makes this removable. Revisit after N2 + N3 land.

### Doc drift
- README.md line 59 references `/tmp/forge-repos/`; code uses `/tmp/phalanx-repos/`. Fixed when A lands.
- docs/MULTI_AGENT_CI_FIXER.md specifies GPT-4.1 for Log Analyst; code uses Claude. Doc refresh pending after CI Fixer brainstorm.

---

## Summary scoreboard

| # | Title | Status |
|---|---|---|
| A | Rebrand cutover | OPEN |
| B | pgvector extension migration | RESOLVED (already in code — audit error) |
| D | OTel wiring / removal | OPEN |
| E | CI-driven deploy | OPEN |
| F | Soul layer scope + agent isolation | OPEN |
| H | Abandoned dirs cleanup | OPEN |
| I+J | DAG mode: graph consolidation + hard-fail | OPEN |
| N1 | Split CI Fixer worker | APPROVED |
| N2 | Sandbox hardening research | RESEARCHING |
| N3 | Sandbox-only validation (no local fallback) | DECIDED |
| N4 | Tier 1 author-branch commit strategy | NEEDS CLARITY |

**Next actions:**
1. Raj: share CTO/founder vision for CI Fixer (brainstorm).
2. Claude: produce N2 sandbox hardening research proposal after vision is captured.
3. Sequentially resolve: N1 → N2/N3 bundle → A → (the rest, in order Raj picks).
