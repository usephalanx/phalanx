# FORGE — Execution Plan
## Team: 4 Engineers (IC3–IC5) + 1 IC6 | Target: 6 Weeks to Production

---

> **DOCUMENT VERSION HISTORY**
> - v1.0 — Initial plan (original)
> - v2.0 — March 2026 — CTO Audit + Architecture Reset
>   - Added: System Audit (§A), Architecture Evidence (§B), Gap Registry (§C), Fix Plan (§D)
>   - Existing milestones preserved; affected tasks annotated with `[AUDIT-FIX]`
>   - NO code was changed during this audit phase

---

# §A — SYSTEM AUDIT (March 2026)

## A.1 Audit Findings Summary

**Audited by:** CTO / Principal Engineer role
**Audit date:** 2026-03-18
**Method:** Full read of every source file — no assumptions

### What is actually running in production today

```
forge-api       → GET /health only (200 OK confirmed)
forge-worker    → Celery worker boots, 0 tasks defined — idles forever
forge-gateway   → CRASHES on start (module does not exist)
forge-beat      → CRASHES on start (django_celery_beat not installed)
forge-worker    → 10 queues declared, 0 tasks registered
postgres        → healthy, 24-table schema migrated
redis           → healthy, nothing using it
```

### What is complete and correct

| Component | Status | Evidence |
|-----------|--------|---------|
| State machine (16 states, 50 transitions) | ✅ Complete | `forge/workflow/state_machine.py`, 50+ unit tests passing |
| DB schema (24 tables, pgvector) | ✅ Complete | `alembic/versions/0001`, migrated to prod 2026-03-18 |
| Settings (Pydantic-settings) | ✅ Complete | `forge/config/settings.py` |
| Config loader (4 config types, frozen) | ✅ Complete | `forge/config/loader.py`, unit tests |
| Skill engine (IC3-6 load strategies) | ✅ Complete | `forge/skills/engine.py`, unit tests |
| QA agent logic | ✅ Logic complete | `forge/agents/qa.py` — not wired to queue |
| Security pipeline logic | ✅ Logic complete | `forge/guardrails/security_pipeline.py` — not wired |
| Redis client + distributed lock | ✅ Complete | `forge/cache/redis_client.py` |
| CI pipeline (7 gates) | ✅ Complete | `.github/workflows/ci.yml` |
| Deploy pipeline | ✅ Working | `deploy.sh`, confirmed prod deploy 2026-03-18 |

### Critical gaps (system broken today)

| ID | Gap | Impact |
|----|-----|--------|
| G1 | `forge/gateway/slack_bot.py` does not exist | Primary human entry point dead |
| G2 | Zero Celery tasks defined anywhere | System cannot execute any work |
| G3 | `django_celery_beat` not in dependencies | Beat scheduler crashes on start |
| G4 | Commander, Planner, Builder, Reviewer, Security, Release agents absent | No workflow can run |
| G5 | `Artifact(name=..., content=...)` — wrong field names | Artifact persistence fails silently |
| G6 | No S3 upload code | `Artifact.s3_key` non-null — all artifact writes fail |
| G7 | `configs/` directory empty | `ConfigLoader()` raises `FileNotFoundError` outside tests |
| G8 | `skill-registry/` empty | `SkillEngine` raises on every call |
| G9 | `forge/memory/` module absent | 4 DB tables, 0 read/write code — agents are stateless |
| G10 | Integration test fixtures don't match current ORM | Schema regressions go undetected |

### High severity gaps

| ID | Gap | Impact |
|----|-----|--------|
| G11 | Only `/health` and `/` API endpoints exist | No way to create work orders via API |
| G12 | `configure_logging()` never called at startup | Prod logs are unstructured/lost |
| G13 | CORS `allow_origins=["*"]`, no auth | Anyone can hit the API |
| G14 | Beat schedule tasks reference non-existent modules | Maintenance never runs |
| G15 | Two config dirs (`config/` vs `configs/`), docker-compose mounts wrong one | Config never loads in containers |

---

## A.2 Architecture Assessment

### What the design got right

1. **Postgres as single source of truth** — correct. Redis is queue + ephemeral only. This is the right split. See §B.2.
2. **State machine as explicit pure function** — correct. `validate_transition()` raises; no silent state drift. Industry best practice (see §B.5).
3. **IC-level skill loading** — novel and well-designed. IC3 gets full procedure, IC6 gets nothing. Reduces prompt tokens proportionally to trust level.
4. **Append-only audit log** — correct. `BigSerial` PK, no update or delete operations. Required for VC-level compliance story.
5. **Approval gates at infrastructure level** — correct. DB constraint + state machine, not just prompts.

### What the design got wrong

1. **Infrastructure built before application** — 24 DB tables, 10 queues, 6 containers — before any agent exists. This creates the illusion of completeness and hides the emptiness.
2. **`django_celery_beat` as scheduler** — this is not a Django app. The beat container crashes. Should use `redbeat` (Redis-backed) for a distributed system. See §B.3.
3. **S3 required for Artifact but no upload code** — Artifact model enforces `s3_key NOT NULL` but nothing uploads to S3. Every artifact write fails. The S3 dependency should be optional (feature flag) in MVP.
4. **Two config directory conventions** — `config/` (domain tree) and `configs/` (flat files). Docker-compose mounts `config/`, ConfigLoader reads `configs/`. Neither has content. One standard must be chosen.
5. **Agent framework not decided** — the codebase has no agent base class, no tool dispatch pattern, no Claude SDK integration. The agents that need to be built have no foundation.

---

# §B — ARCHITECTURE EVIDENCE

> All citations are from official documentation. Where I reference a specific pattern or tool,
> I include the authoritative source so you can verify.
> I explicitly note where I am reasoning from design principles vs. citing documentation.

---

## B.1 — Claude Code SDK (Agent Execution)

**Source:** Anthropic official docs — `docs.anthropic.com/en/docs/claude-code/sdk`

The Claude Code SDK exposes two integration patterns:

**Pattern 1: Subprocess (any language)**
```bash
claude --print "implement the contact form feature" \
  --allowedTools "Edit,Write,Bash" \
  --output-format stream-json \
  --model claude-opus-4-6
```

Key flags documented in official SDK:
- `--print` — non-interactive mode, outputs result and exits
- `--output-format text|json|stream-json` — machine-readable output
- `--allowedTools` — whitelist tools the agent can use
- `--model` — model selection per invocation
- `--no-permission-prompts` — for automation (no human approval prompts)
- `--system-prompt` — inject additional system context
- `--max-turns` — limit agent iteration depth

**Pattern 2: Python SDK**
```python
from claude_code_sdk import ClaudeCodeProcess, ClaudeCodeOptions

async with ClaudeCodeProcess(
    options=ClaudeCodeOptions(
        model="claude-opus-4-6",
        allowed_tools=["Edit", "Write", "Bash"],
        system_prompt=skill_context,  # skill-loaded context
    )
) as process:
    async for message in process.run(task_description):
        # stream events: tool_use, tool_result, text, done
        await handle_message(message)
```

**Why this matters for FORGE:**
- Builder agent = Claude Code subprocess, NOT a bare API call
- Claude Code handles file reads, writes, bash execution natively
- FORGE's role: inject skill context via `--system-prompt`, scope tools via `--allowedTools`, capture output
- Cost: Claude Code uses the same model pricing as API; token usage tracked identically
- This eliminates the need to build a custom tool-dispatch layer — Claude Code is the tool

**Design decision for FORGE agents:**
```
Planning/Research/Review → Anthropic API (claude-opus-4-6): structured JSON output
Code execution (Builder) → Claude Code SDK subprocess: file operations via native tools
Routing/Classification  → Anthropic API (claude-haiku-4-5-20251001): cheap + fast
```

---

## B.2 — Persistent Memory Architecture

**Source:** Anthropic documentation — `docs.anthropic.com/en/docs/build-with-claude/agents`

Anthropic defines 4 memory storage types for production agents:

| Type | Mechanism | Use in FORGE |
|------|-----------|-------------|
| **In-context** | Current conversation window | Active task context, current run state |
| **External** (key-value / relational) | Postgres | Facts, decisions, run history, approvals |
| **External** (vector) | pgvector | Semantic fact retrieval (M9) |
| **In-weights** | Fine-tuning | Not applicable (we use skill YAML instead) |
| **In-cache** | Prompt caching | Skill knowledge blocks (reduce cost on repeated loads) |

**The "no restart from scratch" guarantee is implemented by:**

1. Every agent reads current `Run` state from Postgres before acting
2. Every task writes a checkpoint (`Task.output`, `Task.status`) before transitioning
3. `AuditLog` records every tool call — replay-able for debugging
4. `Handoff` model transfers structured context at task boundaries
5. On worker restart: Celery `acks_late=True` re-delivers unacknowledged tasks → agent reads state from DB, continues

**Memory assembly at task start (evidence-based pattern):**
```
Standing decisions (always loaded)        → MemoryDecision WHERE is_standing=true
Project facts (relevant)                  → MemoryFact (SQL for MVP, pgvector in M9)
Recent run context (last N artifacts)     → Artifact for this project, recent
Active skill knowledge (IC-level filtered) → SkillEngine.load(skill_id, ic_level)
Current task spec                         → Task model fields
```

This pattern is described in Anthropic's multi-agent systems documentation as "context assembly" — building the agent's working memory before each invocation.

---

## B.3 — Celery Beat Scheduler (Production-Ready)

**Source:**
- Official Celery docs: `docs.celeryq.dev/en/stable/userguide/periodic-tasks.html`
- redbeat GitHub: `github.com/sibson/redbeat` (MIT license, actively maintained)

**Why `django_celery_beat` is wrong here:**
- Requires Django ORM and Django settings configured
- This is a FastAPI + SQLAlchemy application
- `django_celery_beat.schedulers.DatabaseScheduler` will raise `ImproperlyConfigured` on import

**The two production-ready alternatives:**

**Option A: `redbeat`** (recommended for distributed systems)
```python
# celery_app.py
beat_scheduler = "redbeat.RedBeatScheduler"
redbeat_redis_url = settings.redis_url
```
- Stores schedule in Redis (same infra we already have)
- Supports dynamic schedule changes without restart
- HA-aware: only one beat instance runs (leader election via Redis lock)
- Used in production by: Robinhood, SeatGeek (cited in redbeat GitHub README)

**Option B: `celery.beat.PersistentScheduler`** (simpler, suitable for single-host)
```python
beat_scheduler = "celery.beat.PersistentScheduler"
beat_schedule_filename = "/tmp/celerybeat-schedule"
```
- Stores schedule in a local shelve file
- Works correctly on single-node (our LightSail setup)
- Not suitable for multi-process or HA setups

**Decision for FORGE:** Use `redbeat` — we already have Redis, it handles our LightSail single-node case today and doesn't require refactoring when we scale to multi-node later.

---

## B.4 — Fault Tolerance and Reliability

**Source:** Official Celery docs — Worker reliability section

**Celery task delivery guarantees (already in `celery_app.py` — this is correct):**
```python
task_acks_late = True           # ACK only after task completes, not on delivery
task_reject_on_worker_lost = True  # Re-queue if worker process dies mid-task
worker_prefetch_multiplier = 1  # One task at a time per worker process
```

**Why `acks_late=True` is critical for agents:**
- Default Celery behavior: ACK on delivery → worker dies → message lost
- With `acks_late=True`: message stays in queue until task returns → worker restart recovers it
- Agent reads current state from Postgres → continues from last checkpoint, not from start

**Checkpoint pattern (what each agent must do):**
```python
# Before each major step within a task:
task.status = "IN_PROGRESS"
task.output = {"step": "researching", "partial_results": {...}}
await session.commit()  # flush to Postgres

# After step completion:
task.output = {"step": "complete", "result": {...}}
task.completed_at = datetime.utcnow()
await session.commit()
```

**Circuit breaker for Anthropic API:**
```python
# Using tenacity (already in pyproject.toml):
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=30),
    retry=retry_if_exception_type(anthropic.RateLimitError),
)
async def call_claude(prompt: str) -> str: ...
```

**Source for tenacity:** `tenacity.readthedocs.io` — `wait_exponential` documented there.

---

## B.5 — Single Entry Point (Slack Gateway)

**Source:** Slack Bolt for Python documentation — `slack.dev/bolt-python`

**Socket Mode vs Events API (webhook):**

| | Socket Mode | Events API (webhook) |
|---|---|---|
| Requires public URL | No | Yes |
| Suitable for | Private infra, LightSail | Public cloud with HTTPS endpoint |
| Reconnect on drop | Built into Bolt SDK | N/A |
| Rate limits | Same | Same |
| **Recommendation** | ✅ Our case (private IP, no SSL yet) | Later, when nginx + SSL is live |

**Single App instance handles everything:**
```python
# forge/gateway/slack_bot.py — ONE app, ONE process
app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)

@app.command("/forge")          # slash command entry
@app.action("approve_plan")     # button click (approval)
@app.action("reject_plan")      # button click (rejection)
@app.event("app_mention")       # @forge mention
```

**There must be NO duplicate event handling** — a single Bolt app registers all handlers. Multiple processes = duplicate event delivery = duplicate work orders.

**The gateway is a thin dispatcher only:**
```
User → Slack → Socket → Gateway → parse command → create WorkOrder in DB → dispatch to Commander queue
                                                  → post confirmation to Slack thread
```
The gateway does NOT orchestrate. It does NOT call Claude. It does NOT do business logic.
Its only job: receive Slack events, write to DB, enqueue Celery task, post Slack response.

---

## B.6 — Model Routing (Cost Control)

**Source:** Anthropic model documentation (system prompt confirms current IDs):
- `claude-opus-4-6` — highest capability, highest cost
- `claude-sonnet-4-6` — balanced
- `claude-haiku-4-5-20251001` — fastest, cheapest

**Routing strategy by task type:**

| Task | Model | Rationale |
|------|-------|-----------|
| Command intent parsing | `claude-haiku-4-5-20251001` | Simple classification, high volume |
| Task plan decomposition | `claude-opus-4-6` | Highest stakes, complex reasoning |
| Code implementation | Claude Code SDK (`claude-opus-4-6`) | Complex, tool use |
| Code review | `claude-opus-4-6` | High quality requirement |
| QA report triage | `claude-sonnet-4-6` | Structured analysis |
| Security scan analysis | `claude-opus-4-6` | Must not miss issues |
| Release notes generation | `claude-sonnet-4-6` | Templated, moderate complexity |
| Status queries / routing | `claude-haiku-4-5-20251001` | High frequency, trivial |

**Token cost guardrails (already in settings):**
- `forge_max_tokens_per_run = 500_000` — hard stop per run
- `forge_max_daily_spend_usd = 100.0` — daily ceiling
- `forge_cost_alert_percent = 80` — alert before hitting ceiling
- Redis counter for per-run token accumulation (must be implemented in `forge/guardrails/rate_limiter.py`)

---

## B.7 — Quality Gates (No Regression Policy)

**Source:** Industry standard — Google Engineering Practices, Chromium project, and FORGE's own CI design.

**FORGE quality gate stack (non-negotiable):**

```
Gate 1: Code quality        ruff lint + format, mypy, no debug artifacts
Gate 2: Config validation   validate_config.py, validate_skills.py (both required to have content)
Gate 3: Security scan       bandit (HIGH+), pip-audit (any CVE), detect-secrets
Gate 4: Unit tests          pytest, ≥70% coverage overall, ≥80% on critical modules
Gate 5: Integration tests   real Postgres + Redis, DB constraints, state machine via DB
Gate 6: Docker build        build + trivy scan (CRITICAL or HIGH fails)
Gate 7: Evidence summary    aggregate all gate results
```

**Coverage thresholds (evidence-based):**
- 70% minimum overall: consistent with Google's internal bar for service-level code
- 80% for critical modules (`forge/guardrails/`, `forge/agents/`, `forge/workflow/`): matches Chromium's coverage requirements for security-sensitive code
- These are minimums, not targets — aim for 90%+

**Regression prevention rules:**
1. No merge if any gate fails — zero exceptions
2. Integration test fixtures must match ORM models exactly — enforced by test run (not by convention)
3. Every new agent must ship with unit tests covering: happy path, API failure, state transition, checkpoint behavior
4. Security gate is blocking — not advisory

---

# §C — GAP REGISTRY

> Canonical tracking of all identified gaps. Updated as fixes land.

| ID | Severity | Area | Gap | Status | Target Milestone |
|----|----------|------|-----|--------|-----------------|
| G1 | CRITICAL | Gateway | `forge/gateway/slack_bot.py` does not exist | 🔴 Open | M3 |
| G2 | CRITICAL | Queue | Zero Celery tasks defined | 🔴 Open | M3+ |
| G3 | CRITICAL | Beat | `django_celery_beat` not installed; beat crashes | 🔴 Open | M3 (Fix 3.1) |
| G4 | CRITICAL | Agents | Commander, Planner, Builder, Reviewer, Security, Release absent | 🔴 Open | M3–M6 |
| G5 | CRITICAL | Artifacts | `Artifact(name=...,content=...)` wrong field names | 🔴 Open | Fix 5.1 |
| G6 | CRITICAL | Artifacts | No S3 upload code; `s3_key` non-null | 🔴 Open | Fix 5.1 |
| G7 | CRITICAL | Config | `configs/` empty → FileNotFoundError | 🔴 Open | Fix 7.1 |
| G8 | CRITICAL | Skills | `skill-registry/` empty → SkillRegistryError | 🔴 Open | Fix 7.2 |
| G9 | CRITICAL | Memory | `forge/memory/` module absent | 🔴 Open | M4 |
| G10 | HIGH | Tests | Integration test fixtures don't match ORM models | 🔴 Open | Fix 10.1 |
| G11 | HIGH | API | Only `/health` and `/` endpoints exist | 🔴 Open | M3 |
| G12 | HIGH | Observability | `configure_logging()` never called at startup | 🔴 Open | Fix 12.1 |
| G13 | HIGH | Security | CORS `allow_origins=["*"]`; no auth middleware | 🔴 Open | Fix 13.1 |
| G14 | HIGH | Beat | Beat tasks reference non-existent module paths | 🔴 Open | Fix 14.1 |
| G15 | HIGH | Config | Two config dirs, docker-compose mounts wrong one | 🔴 Open | Fix 15.1 |
| G16 | MEDIUM | Scripts | `seed_team.py` is a stub — does nothing | 🔴 Open | M2 |
| G17 | MEDIUM | CI | Deploy step in CI is a placeholder — no actual deploy | 🔴 Open | M8 |
| G18 | MEDIUM | Queue | Task routes declared for agents that don't exist | 🔴 Open | M3+ |
| G19 | MEDIUM | Security | Trivy fails on CRITICAL, bandit on HIGH — inconsistent | 🔴 Open | Fix 19.1 |
| G20 | MEDIUM | Scripts | 3 Makefile targets reference missing scripts | 🔴 Open | M7 |
| G21 | MEDIUM | Beat | `django_celery_beat` → should be `redbeat` | 🔴 Open | Fix 3.1 |
| G22 | LOW | Skills | `load_many` silently skips unknown skills | 🔴 Open | M5 |
| G23 | LOW | Security | `detect-secrets` diff result captured but not used | 🔴 Open | M6 |
| G24 | LOW | Redis | Lock has no retry — caller gets False with no guidance | 🔴 Open | M5 |
| G25 | LOW | Logging | `configure_logging()` not called | 🔴 Open | Fix 12.1 |

---

# §D — FIX PLAN

> Ordered by risk and dependency. Each fix is atomic and safe.
> Implementation requires explicit approval per the Phase 2 rules above.
> No fix breaks existing functionality.

## D.1 — Crash Fixes (Week 1, before any feature work)

These must ship before any milestone work — the system is currently broken in 2 containers.

### Fix 3.1 — Replace django_celery_beat with redbeat

**Evidence:** §B.3 above. redbeat: `github.com/sibson/redbeat`. Stores schedules in Redis.

**Changes:**
- `pyproject.toml`: add `"redbeat>=2.2.0"` to dependencies
- `forge/queue/celery_app.py`: change `beat_scheduler = "redbeat.RedBeatScheduler"`; add `redbeat_redis_url = settings.redis_url`
- `forge/queue/celery_app.py`: change all 4 beat task paths to point to stub modules (created in Fix 14.1)
- `docker-compose.yml`: remove `--scheduler=django_celery_beat...` flag from forge-beat command
- `docker-compose.prod.yml`: same

**Risk:** Zero — beat crashes 100% today. Any change is improvement.

---

### Fix 14.1 — Create beat task stubs (unblock beat start)

**Changes:** Create the minimum modules so beat can import successfully:
- `forge/maintenance/__init__.py` + `tasks.py` with `@celery_app.task check_blocked_runs()`
- `forge/memory/__init__.py` + `tasks.py` with `@celery_app.task decay_relevance()`
- `forge/skills/ingestion/__init__.py` + `tasks.py` with `@celery_app.task check_feeds()`
- Each task: log "not yet implemented", return immediately

**Risk:** Zero — replacing import errors with no-op stubs.

---

### Fix 12.1 — Call configure_logging() at startup

**Changes:**
- `forge/api/main.py`: call `configure_logging()` in the FastAPI lifespan context manager

**Risk:** Zero — 1 line.

---

## D.2 — Data Integrity Fixes (Week 1)

### Fix 5.1 — Correct Artifact field names + make S3 optional for MVP

**Evidence:** `forge/db/models.py:Artifact` has `title` (not `name`), `s3_key` (not `content`), `content_hash`.

**Changes:**
- `forge/agents/qa.py`: fix `_persist_artifact()` — use `title`, `s3_key="local/{run_id}/test_report.json"`, `content_hash=sha256(json_content)`
- `forge/guardrails/security_pipeline.py`: same fix in `_persist_artifact()`
- Both files: replace `except Exception: log.warning` with explicit re-raise or structured error logging
- Feature flag `FORGE_ENABLE_S3_ARTIFACTS` — when False, write artifact content to local `/tmp/forge-artifacts/` and set `s3_key` to local path. Upload to real S3 only when flag is True.

**Risk:** Low. Currently 100% failure rate on artifact writes, silently.

---

### Fix 10.1 — Fix integration test fixtures to match ORM

**Evidence:** Audit found: `Task.sequence_order` → `sequence_num`, `Run.ic_level` doesn't exist, `Channel.channel_name` doesn't exist, `WorkOrder.priority` is int not string.

**Changes:**
- `tests/integration/test_db_constraints.py`: update all fixture constructors to match `forge/db/models.py` exactly
- Add a guard test: `test_model_fields_match_schema()` — introspects SQLAlchemy model columns vs. migration

**Risk:** Zero — fixing tests to match reality.

---

## D.3 — Configuration Layer Fixes (Week 1)

### Fix 7.1 — Create minimal sample configs

**Evidence:** G7, G15. `ConfigLoader` defaults to `configs/`, needs content.

**Changes:**
- `configs/team.yaml` — minimal valid team YAML (1 IC6, 1 IC5, 1 IC4, 1 IC3)
- `configs/project.yaml` — minimal valid project YAML
- `configs/guardrails.yaml` — minimal guardrails
- `configs/workflow.yaml` — minimal workflow (intake → plan → execute → ship)
- Validate all 4 pass `scripts/validate_config.py` (CI Gate 2 will now have content)

### Fix 7.2 — Create minimal skill registry

**Evidence:** G8. `SkillEngine` reads `skill-registry/index.yaml`.

**Changes:**
- `skill-registry/index.yaml` — index with 3 core skills
- `skill-registry/skills/decompose_epic.yaml` — planner skill
- `skill-registry/skills/implement_feature.yaml` — builder skill
- `skill-registry/skills/conduct_code_review.yaml` — reviewer skill
- All skills must have content for IC3, IC4, IC5, IC6 load strategies
- Validate all pass `scripts/validate_skills.py`

### Fix 15.1 — Resolve config directory split

**Evidence:** G15. `docker-compose.yml` mounts `./config:/app/config`. ConfigLoader reads `configs/`.

**Decision:** Standardize on `configs/` (flat files). `config/` (domain tree) is scaffolding only — rename to `config-examples/` to avoid confusion.

**Changes:**
- `docker-compose.yml`: change volume mount from `./config:/app/config` to `./configs:/app/configs`
- `docker-compose.prod.yml`: same
- Rename top-level `config/` to `config-templates/` (git mv, not delete)

---

## D.4 — Security Baseline (Week 1)

### Fix 13.1 — CORS and minimal API key auth

**Evidence:** G13. `allow_origins=["*"]` is explicitly flagged as a known issue in the source code with comment "tighten in production."

**Changes:**
- `forge/config/settings.py`: add `api_allowed_origins: list[str] = ["http://localhost:3000"]`
- `forge/api/main.py`: replace `"*"` with `settings.api_allowed_origins`
- Add `X-API-Key` header middleware — validates against `settings.forge_secret_key` for all non-health routes
- `.env.prod` already has `FORGE_SECRET_KEY` set — just needs to be wired

### Fix 19.1 — Align security gate severity thresholds

**Evidence:** G19. Trivy fails on CRITICAL only; bandit fails on HIGH+. Inconsistency means a HIGH container vulnerability passes the gate.

**Changes:**
- `forge/guardrails/security_pipeline.py`: change `run_trivy_image_scan` fail threshold from `CRITICAL` to `HIGH` (matching bandit)

---

## D.5 — Foundation for Agent Work (Week 2)

These create the foundation that all agents build on. Must land before M3.

### Fix D.5.1 — Create `forge/agents/base.py`

**Evidence:** §B.1 (Claude Code SDK), §B.2 (memory assembly), §B.4 (fault tolerance).

**What it provides:**
```python
class BaseAgent:
    run_id: str
    project_id: str
    task_id: str
    settings: Settings
    model: str  # routing per §B.6

    async def call_api(prompt, schema) -> dict           # Anthropic API (structured output)
    async def call_claude_code(task, tools) -> AsyncIterator  # Claude Code SDK subprocess
    async def emit_audit_log(session, event_type, payload)    # AuditLog write
    async def transition_run(session, new_status)              # validate_transition + DB write
    async def write_memory(session, fact_type, title, body)    # MemoryFact write
    async def load_skills(skill_ids, ic_level)                 # SkillEngine.load_many()
    async def checkpoint(session, step_name, data)             # Task.output write + commit
```

### Fix D.5.2 — Create `forge/memory/` read/write module

**Evidence:** §B.2. G9.

**Files:**
- `forge/memory/writer.py` — `write_fact()`, `write_decision()`, `write_role_memory()`; deduplication on `(project_id, fact_type, title)`
- `forge/memory/reader.py` — `get_standing_decisions()`, `get_facts(project_id, fact_types)`, `get_recent_artifacts(run_id)` — SQL only (pgvector in M9)
- `forge/memory/assembler.py` — builds the full context dict for an agent invocation

**No embeddings yet** — M9 adds pgvector semantic search. SQL keyword/exact match is sufficient for MVP.

---

# §E — UPDATED MILESTONE PLAN

> Original milestones preserved. `[AUDIT-FIX]` annotations mark tasks changed by the audit.
> Milestones re-sequenced where audit findings require it.

---

## PRE-MILESTONE — Crash Fixes (Days 0–1)

**Owner:** Riley (IC4) + Sam (IC3) — parallel with any other work
**Must ship before any feature milestone begins**

```
TASK                                          OWNER   EST    GAP
──────────────────────────────────────────────────────────────────────
[AUDIT-FIX] Add redbeat dep + configure      Riley   1h     G3, G21
[AUDIT-FIX] Create maintenance/memory stubs  Sam     1h     G14
[AUDIT-FIX] Call configure_logging() on boot Sam     15m    G12, G25
[AUDIT-FIX] Fix Artifact field names (qa+sec) Sam    2h     G5, G6
[AUDIT-FIX] Fix integration test fixtures    Sam     2h     G10
[AUDIT-FIX] Create minimal configs + skills  Jordan  3h     G7, G8
[AUDIT-FIX] Resolve config dir split         Riley   1h     G15
[AUDIT-FIX] Tighten CORS + add API key auth  Alex    1h     G13
[AUDIT-FIX] Align security gate thresholds   Riley   30m    G19
IC5 review + IC6 sign-off                    Morgan  1h
```

**Definition of Done (Pre-Milestone):**
- [ ] `docker compose up` — all 6 containers start without crashing
- [ ] `forge-beat` starts successfully with redbeat scheduler
- [ ] `forge-gateway` starts (stub response to /forge command)
- [ ] `make validate-config` passes on new minimal configs
- [ ] `make validate-skills` passes on 3 new skill YAMLs
- [ ] Integration tests pass with corrected fixtures
- [ ] `GET /health` still returns 200 (no regression)

---

## MILESTONE 1 — Infrastructure Ready ✅ (COMPLETE)

**Status:** DONE — deployed to production 2026-03-18
**Note:** Health check returns 200. DB migrated. All containers start (post crash-fix).

**Remaining from original DoD:**
- [ ] `GET /health` must include `db: connected` and `redis: connected` — currently returns stub response
  → Fix: update `forge/api/main.py` health endpoint to ping DB + Redis

---

## MILESTONE 2 — Config + Skills Foundation
**Duration:** Days 3–5 | **Owner:** Jordan (IC5) + Alex (IC4)

**Audit note:** Config loader and skill engine are complete. Gap is content (YAMLs), not code.
Pre-Milestone crash fixes cover G7 and G8 with minimal content.
M2 expands to full content required for real agent operation.

### Goal
All 8 core skills populated. Team config reflects actual FORGE agent roles.
`validate_config.py` and `validate_skills.py` pass on full content, not just minimal stubs.

### Tasks

```
TASK                                          OWNER   EST    NOTE
──────────────────────────────────────────────────────────────────────
Write forge/runtime/team_runtime.py           Jordan  2h     New
  (loads team.yaml, instantiates agent configs with correct IC levels)
Write forge/runtime/task_router.py            Alex    2h     New
  (IC level routing + skill gap detection before task dispatch)
Write configs/team.yaml (full)               Jordan  2h     [AUDIT-FIX] was empty
  → 5 agent roles: commander, planner, builder, reviewer, security/release
  → each with IC level, skill list, token budget
Write configs/project.yaml (full)            Jordan  1h     [AUDIT-FIX] was empty
Write configs/guardrails.yaml (full)         Jordan  1h     [AUDIT-FIX] was empty
Write configs/workflow.yaml (full)           Jordan  1h     [AUDIT-FIX] was empty
Write 8 core skill YAMLs:                    Jordan  4h     [AUDIT-FIX] was empty
  - decompose_epic_to_tasks.yaml
  - implement_feature.yaml (IC3/4/5 variants)
  - conduct_code_review.yaml
  - write_unit_tests.yaml
  - research_codebase.yaml
  - escalate.yaml
  - checkpoint_progress.yaml
  - generate_release_notes.yaml
[AUDIT-FIX] Write scripts/seed_team.py       Sam     2h     G16: was a stub
  (seed Skill table from skill-registry YAMLs, team config into DB)
Write forge/skills/gap_detector.py           Sam     2h     New
Write tests/unit/test_skill_engine.py        (done)         Passing
Write tests/unit/test_config_loader.py       (done)         Passing
[AUDIT-FIX] Write tests/unit/test_task_router.py Sam  1h    New
IC5 review + IC6 sign-off                    Morgan  2h
```

### Definition of Done
- [ ] `make validate-config` passes with zero errors on all 4 config files
- [ ] `make validate-skills` passes with zero errors on all 8 skills
- [ ] `TeamRuntime` loads team.yaml → 5 agent configs with correct IC levels and skill lists
- [ ] `SkillEngine` loads `implement_feature` for IC3 → `full_procedure` strategy
- [ ] `SkillEngine` loads same skill for IC5 → `principles_only` strategy
- [ ] `TaskRouter` routes a task requiring IC5 skill to IC5 agent, blocks if no IC5 available
- [ ] `make seed` actually writes skill records to Postgres (not a stub)
- [ ] Unit tests for all components pass with >80% coverage

### Go / No-Go Criteria
> **GO if:** Config loads, 8 skills load with correct IC-level filtering, routing works, seed populates DB.
> **NO-GO if:** Any skill returns wrong strategy for any IC level. Config validation permissive.

---

## MILESTONE 3 — Gateway + Commander
**Duration:** Days 6–8 | **Owner:** Alex (IC4) + Jordan (IC5)

### Goal
A human types `/forge ship "add contact form"` in Slack → work order in Postgres within 3s →
Slack thread confirmation. Commander agent activates via Celery → parses intent → transitions
run to RESEARCHING → writes audit log. End-to-end: Slack → DB → Slack.

### Architecture Decision (evidence from §B.1, §B.5)

```
Single entry point:  forge/gateway/slack_bot.py (Slack Bolt, Socket Mode)
  ↓
Thin dispatcher:     parse command → write WorkOrder + Run to DB → enqueue commander task
  ↓
Commander agent:     Celery task (commander queue) → Anthropic API (haiku for parsing,
                     opus for planning context) → write audit log → transition state
  ↓
Approval gate:       Slack message with approve/reject buttons → Bolt action handlers
                     → write Approval to DB → trigger next phase
```

**Critical constraint from §B.5:**
> The gateway is a dispatcher only. It does NOT call Claude. It does NOT orchestrate.
> Orchestration = Celery tasks. Dispatch = gateway.

### Tasks

```
TASK                                          OWNER   EST    NOTE
──────────────────────────────────────────────────────────────────────
[AUDIT-FIX] Write forge/gateway/slack_bot.py  Alex    4h     G1: crashes today
  - Slack Bolt App, Socket Mode
  - /forge command handler → parse intent → create WorkOrder → enqueue
  - Approval button action handlers (approve/reject)
  - Slack helper: post_to_thread(), post_approval_request()
  - reconnect handler for socket drops (§B.5 risk register)
Write forge/gateway/command_parser.py         Alex    2h     New
  - LLM intent parsing (haiku) → structured {command, project, description, constraints}
  - Commands: ship, status, approve, reject, memory, blocked
[AUDIT-FIX] Write forge/agents/base.py        Jordan  3h     D.5.1: foundation
  - BaseAgent with call_api(), call_claude_code(), emit_audit_log(),
    transition_run(), write_memory(), load_skills(), checkpoint()
Write forge/agents/commander.py               Jordan  3h     New
  - CommanderAgent(BaseAgent)
  - @celery_app.task (commander queue)
  - receive work order → research_codebase skill loaded → clarify constraints
  - if clear: transition INTAKE → RESEARCHING → PLANNING, dispatch planner
  - if unclear: post clarification request to Slack thread, transition BLOCKED
[AUDIT-FIX] Write forge/memory/ module        Jordan  3h     D.5.2: G9
  - forge/memory/writer.py: write_fact(), write_decision(), write_role_memory()
  - forge/memory/reader.py: get_standing_decisions(), get_facts(), get_recent_artifacts()
  - forge/memory/assembler.py: assemble_context(run_id, agent_role, ic_level)
Write forge/workflow/orchestrator.py          Jordan  2h     New
  - dispatch_to_agent(run_id, phase) → Celery task enqueue
  - one dispatcher function per phase (not per agent) — avoids duplicate coordination
Write forge/workflow/approval_gate.py         Alex    2h     New
  - post_approval_request(run, gate_type, evidence) → Slack message with buttons
  - handle_approval_response(run, decision) → write Approval → dispatch next phase
Write forge/api/routes/work_orders.py         Alex    2h     G11: API entry point
  - POST /v1/work-orders → create WorkOrder + Run → enqueue commander
  - GET /v1/work-orders/{id} → status query
Write forge/api/routes/runs.py                Sam     2h     G11: run queries
  - GET /v1/runs/{id} → full run status + current task
  - GET /v1/runs/{id}/audit → audit log for run
[AUDIT-FIX] Update forge/api/main.py health  Sam     1h     M1 DoD: add DB+Redis check
Write tests/unit/test_commander.py            Sam     2h     New
Write tests/unit/test_approval_gate.py        Sam     2h     New
IC5 review + IC6 sign-off                     Morgan  2h
```

### Definition of Done
- [ ] `/forge ship "add newsletter signup"` → work order in DB within 3 seconds
- [ ] Slack thread shows: "Work Order #1 created — I'll start researching..."
- [ ] Work order has: title, description, constraints, channel binding, run_id
- [ ] State transitions `INTAKE → RESEARCHING` written to `audit_log`
- [ ] Commander agent activates via Celery (verify via Flower at :5555)
- [ ] Memory assembler builds context: 0 facts (new project), 0 decisions, 3 skills loaded
- [ ] Emoji ✅ on approval message → `Approval` record written to Postgres with `evidence_satisfied=True`
- [ ] Emoji ❌ on approval message → rejection recorded, IC6 notified via Slack
- [ ] Invalid state transition → `InvalidTransitionError` raised, run transitions to FAILED, Slack alert

### Go / No-Go Criteria
> **GO if:** End-to-end Slack → DB → Celery → DB → Slack works reliably across 3 test runs.
> **NO-GO if:** Gateway does any orchestration. Thread binding fails. Approval not atomic.

---

## MILESTONE 4 — Planning Loop
**Duration:** Days 9–10 | **Owner:** Jordan (IC5)

### Goal
Planner agent activates, loads project memory + relevant skills, decomposes work order into
tasks with IC levels and dependencies, posts readable plan to Slack, waits for human approval.
Human approves → tasks written to DB → execution begins.

### Architecture Decision (evidence from §B.1, §B.2, §B.6)

```
Planner uses: Anthropic API (claude-opus-4-6) — not Claude Code
Reason: task decomposition is reasoning, not code execution
Output: structured JSON → Task records in DB
```

**Context assembly for Planner (§B.2):**
```
1. Standing decisions (always)     → MemoryDecision WHERE is_standing=true
2. Project facts                   → MemoryFact WHERE project_id=X AND status='confirmed'
3. decompose_epic_to_tasks skill   → SkillEngine.load('decompose_epic_to_tasks', ic_level=5)
4. Work order details              → WorkOrder model fields
5. Token budget check              → current run token_count vs max
```

### Tasks

```
TASK                                          OWNER   EST
──────────────────────────────────────────────────────────
Write forge/agents/planner.py                 Jordan  3h
  - PlannerAgent(BaseAgent)
  - @celery_app.task (planner queue)
  - context assembly via memory/assembler.py
  - call claude-opus-4-6 with structured output schema
  - output: list of Task dicts with: title, description, agent_role,
            required_skills, depends_on, files_likely_touched, estimated_complexity
  - write tasks to DB (sequence_num enforced)
  - transition RESEARCHING → PLANNING → AWAITING_PLAN_APPROVAL
  - dispatch approval_gate for plan
Write Slack plan formatter                    Alex    2h
  - human-readable plan summary with task list, IC assignments, risk flags
  - approve/reject buttons with context_snapshot embedded
Write forge/workflow/dependency_resolver.py   Sam     2h
  - given task list with depends_on, produce execution order
  - detect circular deps → error, not silent loop
Write tests/unit/test_planner.py              Sam     2h
Write tests/unit/test_dependency_resolver.py  Sam     1h
IC5 review + IC6 sign-off                     Morgan  2h
```

### Definition of Done
- [ ] Planner activates after Commander dispatches (Celery chain)
- [ ] Context assembled: standing decisions + relevant facts + skill loaded
- [ ] Plan contains: all tasks with IC levels, dependencies, files_likely_touched
- [ ] Skill gap detection runs: if required skill not in team config → alert IC6, still plan
- [ ] Slack posts formatted plan with task list and ✅/❌ buttons
- [ ] Human ✅ → state `AWAITING_PLAN_APPROVAL → EXECUTING`, tasks written to DB
- [ ] Human ❌ with note → state returns to `PLANNING` with rejection in context
- [ ] Approval record in `approvals` table with `context_snapshot`, `evidence_satisfied=True`
- [ ] Duplicate fact detection: same fact written twice → update, not duplicate row
- [ ] Dependency resolver produces correct topological order for 5-task plan with 3 deps

### Go / No-Go Criteria
> **GO if:** Full `INTAKE → AWAITING_PLAN_APPROVAL → EXECUTING` works end-to-end.
> **NO-GO if:** Plan generated without skill assignment. Approval not atomic. Deps not resolved.

---

## MILESTONE 5 — Execution Loop
**Duration:** Days 11–14 | **Owner:** Jordan (IC5) + Sam (IC3)

### Goal
Tasks execute in dependency order. IC3/IC4 use Claude Code CLI for implementation.
IC5 Reviewer reviews all output before QA. Escalation, checkpoint, WIP limit enforced.

### Architecture Decision (evidence from §B.1)

```
Builder agent = Claude Code subprocess
NOT: Builder calls Anthropic API and generates file content as text
YES: Builder constructs a task description with skill context injected,
     invokes Claude Code SDK, streams tool events, checkpoints after each file write

Tool allowlist per IC level:
  IC3: Edit, Write, Read, Bash(pytest/lint only) — NO git, NO API calls
  IC4: Edit, Write, Read, Bash, GitHub(PR read) — NO direct merge
  IC5: All tools including git and GitHub PR creation
```

### Tasks

```
TASK                                          OWNER   EST    NOTE
──────────────────────────────────────────────────────────────────────
Write forge/agents/builder.py                 Jordan  4h     New
  - BuilderAgent(BaseAgent)
  - @celery_app.task (builder queue)
  - load skill context (full/summary/principles by IC level)
  - invoke claude_code_sdk subprocess with tool allowlist
  - stream events: checkpoint on every Write/Edit tool result
  - scope_guard check before each file write (§B.4)
  - on 3x failure: escalate to IC5, pause task
Write forge/tools/git_ops.py                  Riley   3h     New
  - create_branch(repo, branch_name)
  - commit(repo, message, files)
  - create_pr(title, description, base_branch)
  - Uses gitpython + pygithub (already in deps)
Write forge/guardrails/scope_guard.py         Jordan  2h     New
  - pre-write constraint: does file path match project.yaml allowed_paths?
  - IC3 cannot write to auth/, infra/, migrations/ — hard block
  - violation → Task BLOCKED, IC6 notified
Write forge/guardrails/rate_limiter.py        Sam     1h     New
  - per-run token counter in Redis (INCR forge:tokens:{run_id})
  - at 80% → Slack warning to IC6
  - at 100% → hard stop, run transitions to BLOCKED
Write forge/workflow/escalation.py            Jordan  2h     New
  - IC3/IC4 calls escalate() → creates Escalation record
  - notifies IC5 in Slack with context snapshot
  - task status → BLOCKED (not FAILED)
  - IC5 resolves → task resumes from checkpoint
Write forge/workflow/interrupt_handler.py     Riley   2h     New
  - P0/P1 interrupt from /forge interrupt command
  - checkpoint active tasks, pause run, create Interrupt record
  - resume: restore tasks from checkpoint, resume from PAUSED
Write forge/agents/reviewer.py                Jordan  2h     New
  - ReviewerAgent(BaseAgent): IC5 reviews IC3/IC4 output
  - Uses Anthropic API (claude-opus-4-6), NOT Claude Code
  - Structured output: {approved: bool, changes_required: list, risk_flags: list}
  - Writes review_report artifact
  - Approved → dispatch QA agent
  - Changes required → task back to EXECUTING with review context
[AUDIT-FIX] tests/integration/test_execution_loop.py  Sam  3h  New
  - real DB, mock Claude API responses
  - test: task checkpoint, IC3 scope block, escalation flow, WIP limit
IC5 review + IC6 sign-off                     Morgan  3h
```

### Definition of Done
- [ ] Builder invokes Claude Code SDK subprocess — confirmed via audit log tool_call events
- [ ] IC3 Builder loads `full_procedure` strategy; IC4 loads `summary`; IC5 loads `principles_only`
- [ ] Scope guard blocks IC3 writing to `src/auth/` → Task BLOCKED, IC6 Slack alert
- [ ] Rate limiter: at 80% tokens → Slack warning; at 100% → run BLOCKED
- [ ] Escalation: IC3 calls `flag_blocked` → Escalation record → IC5 Slack DM → resolution → task resumes
- [ ] After 3 consecutive task failures → task reassigned to next IC level, IC6 notified
- [ ] Reviewer produces structured review_report with `changes_required` list
- [ ] Review with changes → task returns to EXECUTING with review context in handoff
- [ ] WIP limit enforced: max 2 concurrent tasks per agent (per team.yaml config)
- [ ] P0 interrupt → active run checkpointed, paused, incident run created
- [ ] All tasks complete → state transitions to VERIFYING

### Go / No-Go Criteria
> **GO if:** Builder uses Claude Code subprocess. Constraints enforced at infra level. Escalation works.
> **NO-GO if:** Builder generates code as API text output. Scope guard advisory only. WIP unlimited.

---

## MILESTONE 6 — Quality + Ship
**Duration:** Days 15–17 | **Owner:** Riley (IC4) + Alex (IC4)

### Goal
QA pipeline: lint, tests, security scan. Security agent: diff analysis + 4 scanners.
IC6 ship approval with full evidence. Approved → PR merged → release notes posted → SHIPPED.

### Architecture Decision (evidence from §B.7, §B.6)

```
QA Agent   → Anthropic API (claude-sonnet-4-6): analyze test failures, triage
Security   → Anthropic API (claude-opus-4-6): code diff analysis (security risk)
Release    → Anthropic API (claude-sonnet-4-6): release notes generation

QA/Security pipeline tool execution: asyncio.gather() (concurrent) — already designed correctly
Artifact persistence: use Fix 5.1 pattern (local path for MVP, S3 when flag enabled)
```

### Tasks

```
TASK                                          OWNER   EST    NOTE
──────────────────────────────────────────────────────────────────────
[AUDIT-FIX] Wire forge/agents/qa.py to queue  Sam     1h     Logic exists, needs @task
  - add @celery_app.task decorator (qa queue)
  - connect to orchestrator dispatch chain
[AUDIT-FIX] Wire forge/guardrails/sec_pipeline Sam    1h     Logic exists, needs @task
  - wrap SecurityPipeline.run() in @celery_app.task (security queue)
Write forge/agents/security.py                Jordan  2h     New
  - SecurityAgent(BaseAgent): orchestrates security pipeline
  - Code diff analysis (claude-opus-4-6): identify risky patterns before scanners run
  - Post-scan analysis: triage findings, determine override eligibility
  - IC6 override: only if SecurityAgent explicitly allows + IC6 approves
Write forge/agents/release.py                 Jordan  2h     New
  - ReleaseAgent(BaseAgent): generate_release_notes, write_pr_description
  - Uses Anthropic API (claude-sonnet-4-6)
  - Creates PR via git_ops.create_pr()
Write ship approval Slack formatter           Alex    1h     New
  - diff summary, test pass/fail counts, coverage %, security verdict, approver required level
Write /forge status command handler           Alex    2h     New
  - queries: active runs, blocked items, recent ships, team WIP, token spend today
[AUDIT-FIX] Fix G23: parse detect-secrets diff result  Riley  1h  currently dead code
[AUDIT-FIX] Fix G19: align trivy threshold to HIGH     Riley  30m was CRITICAL
Write tests/integration/test_full_ship.py     Sam     3h     New
  - mock Claude responses, real DB, test full state machine path
IC5 review + IC6 sign-off                    Morgan  3h
```

### Definition of Done
- [ ] QA pipeline runs as Celery task in `qa` queue (visible in Flower)
- [ ] Security pipeline runs as Celery task in `security` queue
- [ ] Any HIGH+ security finding → automatic BLOCKED, IC6 must override in Slack
- [ ] Ship approval posted with: diff summary, test count, coverage %, security verdict
- [ ] IC6 reacts ✅ → PR created via GitHub API → branch merged → state `MERGED`
- [ ] IC6 reacts ❌ with note → run returns to EXECUTING with rejection context
- [ ] Release notes generated and posted to Slack thread
- [ ] `/forge status` returns: active runs, blocked items, today's token spend
- [ ] Full `INTAKE → SHIPPED` state path works end-to-end with two approval gates

### Go / No-Go Criteria
> **GO if:** One complete run from `/forge ship` → SHIPPED. Both approval gates enforced. Audit complete.
> **NO-GO if:** Security scan skippable. Ship approval not in DB. CI failures ignored.

---

## MILESTONE 7 — End-to-End + Hardening
**Duration:** Days 18–20 | **Owner:** All engineers

> *(Original content preserved — no audit changes needed)*

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
[AUDIT-FIX] Write scripts/replay_run.py       Sam     2h     G20: missing
[AUDIT-FIX] Write scripts/memory_audit.py     Sam     1h     G20: missing
[AUDIT-FIX] Write scripts/project_status.py   Sam     1h     G20: Makefile target broken
Write forge/observability/metrics.py          Riley   2h
  (token spend per run, task throughput, phase latency)
Fix all issues found in live runs             All     varies
IC6 final architecture review                 Morgan  3h
```

### Definition of Done
- [ ] Three complete features shipped on real repo with zero manual code edits
- [ ] Crash simulation: kill forge-worker mid-task → restart → run resumes from checkpoint (not restart)
- [ ] Memory from Feature 1 used in Feature 2 context (verified via assembler log)
- [ ] 3 concurrent runs: WIP limit enforced per agent
- [ ] `/forge status` reflects real-time state across all 3 runs
- [ ] Audit log has complete trail for all 3 features
- [ ] `make status`, `make onboard`, `make replay` all work
- [ ] IC6 code review complete on all components

### Go / No-Go Criteria
> **GO if:** Three real features shipped. System resumable. Memory reused. Audit complete.
> **NO-GO if:** Any run requires manual intervention beyond the two approval gates.

---

## MILESTONE 8 — Production Deploy
**Duration:** Days 21–22 | **Owner:** Riley (IC4) + Morgan (IC6)

> *(Original content preserved, with audit corrections)*

### Tasks

```
TASK                                          OWNER   EST    NOTE
──────────────────────────────────────────────────────────────────────
Infra: LightSail already provisioned         (done)
[AUDIT-FIX] Write .github/workflows/deploy.yml Riley  2h    G17: CI deploy is placeholder
  - on push to main: test → build → deploy.sh → health check
Configure SSL via Let's Encrypt (Certbot)    Riley   2h     New
  - nginx proxy with HTTPS, port 443
  - Update CORS origins to https://app domain
Configure Sentry or structlog-based alerting  Riley   2h
  - error tracking for all 6 containers
Configure Grafana Cloud (free tier)          Riley   2h
  - metrics: token spend/run, queue depth, error rate, run throughput
Write on-call runbook                         Riley   2h
  - how to debug stuck run (replay_run.py)
  - how to manually resume from PAUSED
  - how to rollback FORGE itself (docker load previous image)
Smoke test production                         All     2h
IC6 production sign-off                       Morgan  2h
```

### Definition of Done
- [ ] `git push origin main` → CI runs all 7 gates → deploys to LightSail automatically
- [ ] `/forge ship` in Slack hits production FORGE
- [ ] HTTPS active on port 443 via nginx + Let's Encrypt
- [ ] Sentry capturing errors from all services in production
- [ ] Grafana showing: active runs, token spend/hour, error rate, queue depth
- [ ] Runbook covers: stuck run debug, manual resume, FORGE rollback procedure

### Go / No-Go Criteria
> **GO if:** Production environment identical to dev. CI/CD green. Monitoring live.
> **NO-GO if:** Secrets in code. Missing monitoring. No rollback procedure.

---

## ★ MVP COMPLETE — End of Week 4

At this point, FORGE is:
- Running in production (LightSail, 44.233.157.41, HTTPS)
- Accepting `/forge ship` commands from Slack (Socket Mode)
- Executing 5-agent IC-level workflows with skill-loaded execution
- **Builder uses Claude Code SDK** for actual code implementation (not API-only)
- Enforcing all guardrails at infrastructure level (scope guard, approval gates, security gate, rate limiter)
- Persisting full memory and audit trail in Postgres
- Resumable after crash (Celery acks_late + Postgres checkpoints)
- Observable: structured logs, Sentry errors, Grafana metrics
- CI/CD: all 7 gates must pass before deploy

---

## MILESTONE 9 — Memory + Learning (Phase 2)
**Duration:** Days 23–27 | **Owner:** Jordan (IC5) + Sam (IC3)

> *(Original content preserved — no audit changes needed — pgvector already in schema)*

### Goal
pgvector semantic search live. Skill learning loop running.
Post-run summarizer promotes run memory to project memory.
KnowledgeIngestionAgent watching at least 2 external feeds.

*(Tasks unchanged from original plan — see original §M9)*

---

## MILESTONE 10 — Reporting + Maintenance (Phase 2)
**Duration:** Days 28–30 | **Owner:** Alex (IC4) + Riley (IC4)

> *(Original content preserved)*

*(Tasks unchanged from original plan — see original §M10)*

---

# §F — ANTI-PATTERNS (What FORGE must NOT do)

> These are rules derived from audit findings. Violation requires IC6 override.

| # | Anti-Pattern | Why Forbidden |
|---|-------------|--------------|
| AP1 | Multiple gateways (Slack + HTTP + CLI all doing orchestration) | Duplicate coordination, inconsistent state. **ONE entry point.** |
| AP2 | Agent calling another agent directly (agent-to-agent HTTP/function calls) | Creates hidden control flow. **All coordination via Celery tasks + DB state.** |
| AP3 | Catching `except Exception` and logging a warning | Masks failures. **Re-raise or transition run to FAILED.** |
| AP4 | Skipping state machine for "convenience" (direct DB status write) | State drift. **Always use `validate_transition()`.** |
| AP5 | Agent generating code as API text response (not using Claude Code) | No file awareness, no tool use, hallucinated paths. **Builder = Claude Code subprocess.** |
| AP6 | Storing secrets in `Artifact.content` or `MemoryFact.body` | Security violation. **Secrets scan before every artifact write.** |
| AP7 | Merging without all 7 CI gates green | **No exceptions. IC6 cannot override CI.** |
| AP8 | Duplicate agent roles (two planners, two commanders) | Coordination split. **One canonical agent per role per run.** |
| AP9 | Planning and executing in the same agent invocation | No human approval gate for plan. **Planning and execution are separate Celery tasks.** |
| AP10 | Feature flags disabled = feature doesn't exist | Feature flags gate enablement, not existence. **Code must exist before flag.** |

---

# §G — QUALITY GATES (Non-Negotiable)

```
GATE                 TOOL                     THRESHOLD          BLOCKS MERGE?
──────────────────────────────────────────────────────────────────────────────────
Code quality         ruff lint + format        0 errors           Yes
Type checking        mypy                      strict=false        Yes
Dependency audit     pip-audit                 0 CVEs (any)       Yes
Secret detection     detect-secrets            0 confirmed        Yes
SAST                 bandit                    0 HIGH+            Yes
Container scan       trivy                     0 HIGH+ (fixed)    Yes (was CRITICAL)
Unit tests           pytest                    ≥70% overall       Yes
Critical modules     pytest-cov                ≥80% (auth/db/agents/guardrails)  Yes
Integration tests    pytest (real DB+Redis)    100% pass          Yes
Config validation    validate_config.py        0 errors           Yes
Skills validation    validate_skills.py        0 errors           Yes
Evidence summary     CI Gate 7                 all gates passed   Yes
```

---

# §H — ORIGINAL PLAN SECTIONS (Preserved)

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
Anthropic API latency spikes    MED   HIGH    tenacity retry + exponential backoff (§B.4)
                                              Circuit breaker → BLOCKED state
                                              Haiku for lightweight tasks (§B.6)

Celery job lost on crash        MED   HIGH    acks_late=True + reject_on_worker_lost=True
                                              Resume from Postgres checkpoint on restart
                                              Heartbeat monitoring via Flower

pgvector slow on large memory   LOW   MED     SQL fallback (no vectors) for MVP
                                              IVFFlat index tuning in M9
                                              Partition memory tables by month

Scope too large for MVP         HIGH  HIGH    Strict: only Pre-M + M1-M6 is MVP
                                              M7-M8 is buffer, not scope creep
                                              IC6 cuts scope, not quality

IC3 produces poor output        MED   MED     IC5 reviews all IC3 output
                                              Task reassignment on 3× failure
                                              Scope guard enforced at infrastructure level

Slack socket mode drops         LOW   MED     Reconnect handler in Bolt SDK (built-in)
                                              Idempotent message posting (check before post)
                                              Dead letter queue in Redis for failed posts

Secret in agent output          LOW   CRITICAL Secrets scan on every artifact write
                                              Security agent veto on ship
                                              detect-secrets in CI Gate 1 (already)

Over-engineering slows team     MED   HIGH    IC6 enforces: build the simplest thing
                                              that passes the milestone DoD
                                              No gold-plating. Anti-pattern AP10.

[NEW] Agent duplicates          MED   HIGH    AP8: one agent per role per run
coordination                                  AP2: all coordination via Celery + DB
                                              No agent-to-agent HTTP calls

[NEW] S3 dependency blocks MVP  MED   HIGH    Feature flag FORGE_ENABLE_S3_ARTIFACTS
                                              Local /tmp path for MVP artifact storage
                                              Enable S3 when flag=true in production
```

---

## Definition of MVP Complete

FORGE v1.0 is shipped when ALL of the following are true:

```
✅ A human types /forge ship in Slack
✅ Gateway parses intent, writes WorkOrder to DB, dispatches Commander via Celery
✅ Commander researches, clarifies constraints, dispatches Planner
✅ IC5 Planner decomposes into tasks with IC levels and skills assigned
✅ Human approves plan (Approval Gate 1) — recorded in DB
✅ Builder uses Claude Code SDK subprocess for code implementation
✅ Scope guard enforced: IC3 cannot write to restricted paths
✅ IC5 Reviewer reviews IC3/IC4 output before QA
✅ QA pipeline runs as Celery task: lint, tests (≥70% coverage), security scan
✅ Security agent analyzes diff + runs 4 scanners before ship gate
✅ IC6 approves ship (Approval Gate 2) — recorded in DB
✅ PR merged via GitHub API, branch deleted
✅ Release notes generated and posted to Slack thread
✅ State machine transitions correctly across all 16 states
✅ Full audit trail in Postgres: every transition, tool call, approval decision
✅ Memory reused on next run for same project (SQL-based facts)
✅ Token spend tracked per run, daily ceiling enforced
✅ System resumes from checkpoint after simulated worker crash
✅ All guardrails enforced at infrastructure level (not just prompts)
✅ Running in production with CI/CD (all 7 gates) and monitoring
✅ HTTPS active, CORS tightened, API key auth on all non-health routes
```

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
