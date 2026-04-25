# CI Fixer v3 — humanize canary retro

**Date:** 2026-04-24
**Outcome:** v3 closed PR #2 on `usephalanx/humanize` (fork of
`python-humanize/humanize`) with commit
[`75b624a`](https://github.com/usephalanx/humanize/commit/75b624a) — first
external-repo proof point. **8 canary iterations**, **8 distinct bugs**,
**8 deploy cycles** (~2.5 hours of clock time).

This document captures every bug, the pattern that connects them, and
the local integration harness that should have existed before the first
canary attempt.

---

## The 8 bugs, in order

| # | Symptom | Root cause | Fix | Class |
|---|---|---|---|---|
| 1 | Worker logged `Received unregistered task of type 'phalanx.agents.cifix_commander.execute_run'` | `Celery(include=[…])` never imported the 4 v3 agent modules. Queue subscription alone doesn't trigger `@celery_app.task` registration. | Added 4 entries to `phalanx/queue/celery_app.py` `include` list. | **v3-forgot-bootstrap-step** |
| 2 | `TypeError: CIFixCommanderAgent._audit() missing 1 required positional argument: 'event'` | I overrode `_audit` with a wrapper using parameter name `event`; `BaseAgent._audit` takes `event_type`. `_transition_run` invoked the override with kwarg `event_type=…`. | Deleted the override entirely. Commander now inherits `BaseAgent._audit`. | **shadowing-base-with-incompatible-shape** |
| 3 | All 4 v3 Tasks stayed `IN_PROGRESS` forever; advance_run never moved past iteration 1's first agent. | Build-flow agents inline `UPDATE tasks SET status='COMPLETED'…` at the end of `execute()`. v3's leaf agents returned `AgentResult` but never wrote the row. | New module `phalanx/ci_fixer_v3/task_lifecycle.py` with `persist_task_completion(task_id, result)`. Called from each Celery wrapper. | **v3-forgot-bootstrap-step** |
| 4 | Tech Lead failed: `LLM stopped without a valid JSON fix_spec block` | Strict parser only accepted ```json``` fenced blocks. Real GPT-5.4 output had unfenced JSON / mixed prose. | Hardened `_parse_fix_spec_from_text` to try four extraction strategies (fenced, unlabeled fence, bare JSON, brace-balance scan); last-valid-wins. | **strict-parser-meets-stochastic-output** |
| 5 | Tech Lead failed: OpenAI Responses API `400 Invalid value: 'tool'. Supported values are 'assistant', 'system', 'developer', 'user'` | My `_tool_result_message` used `{"role": "tool", "tool_use_id": …, "content": json.dumps(dict)}`. The Responses API needs `{"role": "user", "content": [{"type": "tool_result", …}]}`. | Copied `v2/agent.py:_tool_result_message` verbatim. | **shape-mismatch-when-copying-v2** |
| 6 | Engineer failed: `NotImplementedError: Sonnet LLM wiring lands in Week 1.7. Tests must patch _call_sonnet_llm` | `run_coder_subagent(…)` defaults `llm_call=None` → hits a test-only stub. v2's bootstrap builds the callable via `build_sonnet_coder_callable` and passes it. | Added the same wiring in `cifix_engineer.execute()` before invoking `run_coder_subagent`. | **v3-forgot-bootstrap-step** |
| 7 | Tech Lead set `failing_command = "/opt/.../prek run --all-files"` (the outer CI wrapper). Engineer's narrow fix passed `ruff check`, but the wrapper failed because the sandbox lacks `libatomic.so.1` for prek's Node hooks. | Prompt said "the exact command from fetch_ci_log" — GPT-5.4 picked the literal CI invocation rather than the narrow check. | Tightened TL prompt: pick the NARROWEST command that re-runs JUST the failing check; explicit "DO NOT" list of common wrappers (prek, pre-commit, make, tox, npm test, …). | **prompt-imprecise-for-real-CI** |
| 8 | Iteration 2 patched 3 `.github/workflows/*.yml` files to swap `astral-sh/setup-uv` for `pip install tox`. SRE-verify went green; commit shipped — but a maintainer would reject this as a regression. | TL had no explicit policy on "if the failure is a sandbox env mismatch, escalate; do not edit CI infra." Sandbox couldn't run `uv` so TL "fixed" CI by removing `uv`. | Added prompt section: NEVER patch `.github/workflows/`, `tox.ini`, `Makefile`, `package.json` scripts, `pre-commit-config.yaml`, etc. Set `confidence=0.0` and put the env mismatch in `open_questions`. | **prompt-missing-escalation-policy** |

Plus three **infrastructure incidents** parallel to the bug list:
- Docker daemon wedged twice during the 8 deploys (~20 min lost to manual Docker restart). Same symptom we hit during the Java row.
- Some long-running fetchsandbox builds were hoarding Docker resources.
- One celery task retry caused state-confusion; my idempotency fix (load Run if exists, skip ceremony if `status != INTAKE`) handled it.

---

## The pattern

**Six of the eight bugs were "v3 forgot something v2's bootstrap or
inline code does correctly."** The diff between v3 and v2 isn't really
"v3 is a different architecture" — it's "v3 reuses v2's libraries but
needed to recreate the bootstrap glue v2 had inline." Every missing
piece of glue surfaced as one canary attempt = one prod deploy = ~12
minutes of feedback loop for a problem a 30-second local test would
have caught.

The two outliers (#7, #8) are prompt-engineering issues, not bootstrap
omissions. Different class but same lesson: real-world data exposes
gaps that synthetic / unit tests don't.

---

## The local integration harness that should have existed

What's needed: a script that takes a fixture (recorded CI failure
event) and runs the full v3 DAG **in-process**, with these substitutions:
- **OpenAI / Anthropic** → mocked callables that return canned LLM
  responses (the real API shape, not stubs that raise NotImplementedError).
- **Docker** → either a real local daemon OR a mock that records
  `docker run` / `docker exec` invocations. Real docker is fine for
  high-fidelity test; the mock is what catches shape-mismatch bugs
  like #5 (tool_result format) without burning real API tokens.
- **GitHub** → mock for `fetch_ci_log` and the like; commit_and_push
  goes to a temp local git repo, not GitHub.
- **Postgres** → real (Phalanx already runs against postgres; harness
  uses a clean DB schema per run).
- **Celery** → `CELERY_TASK_ALWAYS_EAGER=True` so tasks run
  synchronously in-process. The harness then asserts on Run + Task
  rows after the synchronous run finishes.

**Acceptance test (canary equivalent):**
1. Insert a fake CIFixRun + CIIntegration row pointing at a small
   pre-baked repo fixture.
2. Dispatch `cifix_commander` synchronously.
3. Within 30 seconds, the harness inspects Run + Tasks, asserts:
   - 4 Tasks created (sre_setup, techlead, engineer, sre_verify)
   - All 4 reach status=COMPLETED
   - Run reaches SHIPPED
   - Engineer's Task.output has `committed=True`

Bugs #1, #2, #3, #5, #6 would all surface in this harness because
they manifest at the Celery-task / agent-shape / DB-row level — none
require real LLM output. Bug #4 partially surfaces (parser tested
against real LLM responses → would catch it after the first Tech Lead
mock returns an unusual shape). Bugs #7 and #8 require real LLM +
real repo, so they remain canary-territory — but the canary feedback
loop after the harness is much shorter (we're only catching
prompt issues, not infra issues).

**Where to build it:** `tests/integration/v3_canary.py`. ~300 LOC.
1-2 days of focused work.

**Cost-benefit:** every bug discovered in the harness saves ~12
minutes (one prod deploy cycle). 6 of the 8 bugs would have been
caught locally → 72 minutes of deploy time avoided per future v3
feature shipment that touches the agent surface. Plus avoided LLM
spend (~$0.50 per failed canary).

---

## Lessons applied to the v3 codebase as part of this retro

1. **`celery_app.py` includes a tripwire comment** flagging that future
   agent additions need entries in BOTH the queue list AND the
   `include=` list. (commit `1453d45`)
2. **`_audit` override deleted** — defensive overrides that wrap base
   methods with mismatched signatures are worse than no override. v3
   commander now inherits `BaseAgent._audit` directly. (commit `26f1408`)
3. **`task_lifecycle.persist_task_completion`** is called from every v3
   Celery wrapper. (commit `d386186`)
4. **`_parse_fix_spec_from_text`** has 4 extraction strategies and
   last-valid-wins semantics. (commit `cbfd823`)
5. **`_tool_result_message`** in TL is verbatim copy of v2's shape with
   a comment explaining why. (commit `5bf10b1`)
6. **TL prompt** has explicit DO/DO NOT lists for `failing_command`
   wrappers. (commit `f8938bc`)
7. **TL prompt** explicitly forbids editing CI infrastructure files
   when sandbox is the mismatch. (this commit)

---

## What we still owe (not in this retro's scope)

- ~~**Local integration harness**~~ ✅ Built — see
  `tests/integration/v3_harness/` (51 tests, 1.1s total runtime,
  no Postgres or Docker required). Catches bug classes #1, #3, #4,
  #7-partial directly; #2/#5/#6 still need a Tier-2 harness with
  real Postgres and real provider calls (deferred).
- **Path 1 (fat base image)** for env_detector. Switching from
  `python:3.10-slim` to `catthehacker/ubuntu:act-22.04` would have
  prevented bug #7's libatomic miss. Tracked as a Phase-2 item.
- **v3 SHIP chain doesn't post a Slack/PR comment** the way build-flow
  does. The humanize PR #2 has the commit but no agent-authored note
  explaining what was done. Worth adding before more external canaries.
- **Cost tracking per-run** isn't surfaced on the scorecard yet for v3
  runs (we hardcoded humanize's 13,752 tokens; need DB query path).

---

## Postscript

The first commit on an external repo that we don't own ([`75b624a`](https://github.com/usephalanx/humanize/commit/75b624a))
shipped 14 hours after the v3 design conversation started, with 8
prod deploys, 7 small fixes, and one ~30-minute Docker-daemon
incident in between. The system architecturally works. The
operational discipline around it (local harness, deploy cadence,
prompt-engineering review) is the next thing to mature.
