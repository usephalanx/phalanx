# FORGE — API Cost & Usage Audit

**Date**: 2026-03-22
**Scope**: All external API usage, token tracking, and cost visibility gaps
**Purpose**: Audit document for financial oversight and burn-rate control

---

## 1. Executive Summary

FORGE makes API calls to two paid LLM providers (Anthropic and OpenAI) on every pipeline run. Token usage is partially logged to structlog but **never aggregated to cost in USD**. The `runs.estimated_cost_usd` and `runs.token_count` columns exist in the DB schema but are never populated. There is no daily spend enforcement, no alerting at threshold, and no dashboard for burn-rate visibility. This document captures the current state and recommended enhancements.

---

## 2. API Usage Inventory

### 2.1 Anthropic (Claude) — Primary LLM

**Model**: `claude-opus-4-6` (default), `claude-haiku-4-5-20251001` (fast/cheap path)
**Auth**: `ANTHROPIC_API_KEY` env var
**Pricing (as of 2026)**: Opus ~$0.015/1K input, ~$0.075/1K output; Haiku ~$0.00025/1K input, ~$0.00125/1K output

| Agent | File | Model | Max Output Tokens | Calls Per Run |
|-------|------|-------|-------------------|---------------|
| Commander | `agents/commander.py` | claude-opus-4-6 | 4,000 | 1 |
| Planner | `agents/planner.py` | claude-opus-4-6 | 6,000 | 1 per task |
| Builder | `agents/builder.py` | claude-opus-4-6 | **16,000** | 1 per task |
| Reviewer | `agents/reviewer.py` | claude-opus-4-6 | 4,096 | 1 per task |
| Release | `agents/release.py` | claude-opus-4-6 | 2,048 | 1 |
| ProductManager | `agents/product_manager.py` | **claude-haiku** | 4,096 | 1 |
| TechLead | `agents/tech_lead.py` | claude-opus-4-6 | 4,000 | 1 |
| IntegrationWiring | `agents/integration_wiring.py` | claude-opus-4-6 | 4,000 | conditional |

**Per-run budget cap**: 500,000 tokens (`forge_max_tokens_per_run` setting)
**Hard SDK ceiling**: 21,333 max_tokens per call for non-streaming (enforced by Anthropic SDK)

**Estimated cost per typical 16-task run** (rough):
- Commander: ~2K input + 4K output = ~$0.33
- 8 builder tasks × (8K input + 8K output avg) = ~$8.40
- 4 planner tasks × (4K input + 4K output) = ~$2.40
- Reviewer + Release: ~$0.50
- **Rough total: $10–15 per successful run**

### 2.2 OpenAI (GPT-4o) — Enrichment Pipeline Only

**Model**: `gpt-4o` (hardcoded in `openai_client.py`)
**Auth**: `OPENAI_API_KEY` env var
**Pricing**: ~$0.0025/1K input, ~$0.01/1K output
**When**: Runs once per WorkOrder before the Claude pipeline starts (in `PromptEnricher`)

| Stage | Agent | Max Tokens | Calls Per Work Order |
|-------|-------|-----------|----------------------|
| Stage 0 — Intent routing | `intent_router.py` | 4,096 | 1 |
| Stage 1 — Requirement normalization | `requirement_normalizer.py` | 4,096 | 1 |
| Stage 2 — Execution planning | `execution_planner.py` | 4,096 | 1 |
| Stage 3 — Dry-run validation | `dry_run_validator.py` | 4,096 | 1–3 (retry loop) |

**Max retries on validation failure**: 2 (so up to 3 planner + 3 validator calls = 6 GPT-4o calls)
**Estimated cost per work order**: ~$0.10–0.30

### 2.3 GitHub API

**Library**: PyGithub
**Auth**: `GITHUB_TOKEN` env var
**Usage**: 1 PR creation per run at release stage
**Cost**: Free (included in GitHub plan)

### 2.4 Slack API

**Libraries**: `slack-bolt`, `slack-sdk`
**Auth**: `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`
**Usage**: Socket Mode (always-on), N approval gate messages per run
**Cost**: Free (included in Slack plan)

### 2.5 AWS S3

**Auth**: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
**Status**: Configured but **not active in current MVP** — all artifacts use `local/` prefix placeholder keys
**Cost**: $0 currently

---

## 3. Current Token Tracking State

### What IS tracked today

| Where | What | How |
|-------|------|-----|
| structlog | Per-call: input_tokens, output_tokens, budget_remaining | `base._call_claude()` logs every call |
| `audit_log.tokens_used` | Per-agent-action token count | Written by `BaseAgent._audit()` |
| `AgentResult.tokens_used` | Total tokens used by agent for task | Returned and logged by each agent |
| OpenAI call logs | input_tokens, output_tokens | `openai_client.py` logs via structlog |

### What is NOT tracked (gaps)

| Gap | Impact | DB Column Status |
|-----|--------|-----------------|
| `runs.token_count` never updated | Can't query total tokens per run | Column exists, always 0 |
| `runs.estimated_cost_usd` never calculated | No USD cost per run | Column exists, always 0.0 |
| No pricing constants anywhere in code | Can't convert tokens → $ | Missing entirely |
| `forge_max_daily_spend_usd = 100.0` not enforced | Daily cap is a no-op | Setting defined, never checked |
| `forge_cost_alert_percent = 80` not enforced | No alerting | Setting defined, never checked |
| OpenAI usage not aggregated to run | GPT-4o spend invisible in DB | No column for it |
| No per-project cost rollup | Can't audit cost per project | Not implemented |

---

## 4. Celery Time Limits

| Agent | Soft Limit | Hard Kill |
|-------|-----------|-----------|
| commander | 3,600s (1h) | none |
| builder | 1,800s (30m) | 3,600s (1h) |
| reviewer | none | none |
| release | none | none |
| security | none | none |
| qa | none | none |
| verifier | 300s (5m) | 360s (6m) |
| integration_wiring | 300s (5m) | 360s (6m) |

**Gap**: reviewer, release, security, qa have no time limits — a hung task will occupy a worker indefinitely.

---

## 5. Recommended Enhancements

### P0 — Cost Visibility (Blocking for Audit)

**5.1 Populate `runs.token_count` and `runs.estimated_cost_usd`**

After each agent task completes, aggregate its token usage back to the parent run. At run completion, calculate USD cost using Anthropic/OpenAI pricing tables and write to DB.

```python
# Suggested pricing constants in phalanx/config/pricing.py
ANTHROPIC_OPUS_INPUT_PER_1K  = 0.015
ANTHROPIC_OPUS_OUTPUT_PER_1K = 0.075
ANTHROPIC_HAIKU_INPUT_PER_1K = 0.00025
ANTHROPIC_HAIKU_OUTPUT_PER_1K = 0.00125
OPENAI_GPT4O_INPUT_PER_1K    = 0.0025
OPENAI_GPT4O_OUTPUT_PER_1K   = 0.010
```

**5.2 Add per-task token columns to `tasks` table**

```sql
ALTER TABLE tasks ADD COLUMN input_tokens INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN output_tokens INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN estimated_cost_usd FLOAT DEFAULT 0.0;
```

This allows per-task cost breakdown, not just per-run totals.

**5.3 Track OpenAI tokens separately**

Add `openai_tokens_in`, `openai_tokens_out`, `openai_cost_usd` to `work_orders` table — enrichment cost should be attributed to the work order, not the run.

---

### P1 — Budget Enforcement

**5.4 Enforce `forge_max_daily_spend_usd`**

Currently a dead config value. Add a daily spend check before every Commander task dispatch:

```python
# In commander before _generate_task_plan()
daily_spend = await _get_daily_spend_usd(session)
if daily_spend >= settings.forge_max_daily_spend_usd:
    raise RuntimeError(f"Daily spend limit ${settings.forge_max_daily_spend_usd} reached")
```

**5.5 Alert at `forge_cost_alert_percent` threshold**

Post a Slack message to the ops channel when daily spend hits 80% of cap.

**5.6 Add per-run cost cap**

Currently only a token count cap (`forge_max_tokens_per_run = 500K`). Add a USD equivalent: stop a run if it will exceed e.g. $50 mid-execution.

---

### P2 — Observability

**5.7 Cost summary in Slack plan approval message**

When Commander posts the plan for approval, include an estimated cost:

```
📋 Plan: 16 tasks | Est. cost: ~$12–18 | Est. time: 45 min
[Approve] [Reject]
```

**5.8 Cost summary in Slack ship approval message**

At the end of a run, include actual token/cost totals:

```
✅ Build complete | Tokens: 184,321 | Cost: $13.42
[Ship] [Reject]
```

**5.9 `/phalanx status` command showing burn rate**

Add a Slack command that shows:
- Today's spend (USD)
- This month's spend
- Total runs completed
- Average cost per run

**5.10 Structured cost log per run**

Emit a single structured log event at run completion with all cost fields, making it easy to stream into a logging backend (Datadog, CloudWatch, etc.):

```json
{
  "event": "run.completed",
  "run_id": "...",
  "anthropic_input_tokens": 95000,
  "anthropic_output_tokens": 42000,
  "anthropic_cost_usd": 11.43,
  "openai_input_tokens": 8200,
  "openai_output_tokens": 3100,
  "openai_cost_usd": 0.052,
  "total_cost_usd": 11.48
}
```

---

### P3 — Cost Optimization

**5.11 Use Haiku for reviewer instead of Opus**

The reviewer does structured JSON analysis (pass/fail + comments). Haiku can handle this at 60× cheaper cost. Potential saving: ~$1–2 per run.

**5.12 Use Haiku for release notes**

Release notes generation is simple summarization. Haiku is sufficient and 60× cheaper. Potential saving: ~$0.15 per run.

**5.13 Cache enrichment results**

GPT-4o enrichment runs on every WorkOrder. For near-identical prompts (e.g. `/phalanx build "web app for dentist"` submitted twice), cache the enrichment result and skip the 4-6 GPT-4o calls.

**5.14 Add soft_time_limit to all agent Celery tasks**

Reviewer, release, security, and qa currently have no time limits. A runaway task could burn tokens indefinitely. Recommend 900s soft / 1800s hard for all agents.

---

## 6. Priority Summary

| Priority | Enhancement | Est. Effort | Impact |
|----------|-------------|-------------|--------|
| P0 | Populate runs.token_count + runs.estimated_cost_usd | 2 days | Unblocks all financial reporting |
| P0 | Add pricing constants + cost calculation utility | 0.5 days | Enables USD tracking |
| P0 | Per-task token columns in DB | 1 day | Granular cost attribution |
| P1 | Enforce daily spend cap | 1 day | Prevents runaway spend |
| P1 | Cost in Slack approval messages | 1 day | Operator visibility at decision points |
| P2 | `/phalanx status` cost command | 1 day | Real-time burn rate visibility |
| P2 | Structured cost log at run completion | 0.5 days | Logging backend integration |
| P3 | Haiku for reviewer + release | 1 day | ~15–20% cost reduction per run |
| P3 | Enrichment result caching | 2 days | Eliminates repeat GPT-4o spend |
| P3 | Soft limits on all agent tasks | 0.5 days | Prevents hung worker spend |

---

## 7. Current Spend Estimate (Back-of-Envelope)

Based on typical run profile (16 tasks, mix of planner + builder + reviewer):

| Cost Driver | Per Run | Per 10 Runs/Day | Per Month (300 runs) |
|-------------|---------|-----------------|----------------------|
| Anthropic (Opus) | $10–15 | $100–150/day | $3,000–4,500 |
| OpenAI (GPT-4o enrichment) | $0.10–0.30 | $1–3/day | $30–90 |
| GitHub API | $0 | $0 | $0 |
| Slack API | $0 | $0 | $0 |
| **Total** | **~$10–15** | **~$100–153** | **~$3,030–4,590** |

> ⚠️ These are estimates based on max_tokens settings and typical prompt sizes. Actual spend requires P0 tracking to be implemented for accurate numbers.

---

*Document prepared for internal audit. Implement P0 items before next external review.*
