# B — a pipeline engine for API integration bugs

**Status:** design, unbuilt. Requires A (brain wiring) shipped and the eval corpus frozen.
**Scope:** the analysis engine behind `/v1/find_bugs` and `/v1/fix_bug`. Nothing else.
**Companion:** [fs-phalanx-proof-contract-v1.md](fs-phalanx-proof-contract-v1.md) — the
boundary rules here are that document's, unchanged.

---

## 1. Why B exists

A (brain wiring) makes today's single guess better-informed. It cannot change what
that guess *is*: one `claude -p` subprocess with `--allowedTools Read,Grep,Glob`,
one shot, no review, no iteration. Three ceilings follow, and no prompt fixes any of them.

**No second opinion.** The CI Fixer deliberately runs its Challenger on a different
model family with clean context, because a model asked to review its own work
flatters it (Panickssery 2024, cited in `cifix_challenger.py`). The API path has one
model marking its own homework.

**No iteration inside the system.** The 2026-08-04 sweep caught this precisely: the
resend fix added a plausible `assert_sendable()` guard that never worked — dead code,
because nothing updated `email_status`. `prove_fix` returned `fix_incomplete`, the
agent iterated, and the second fix greened. **That loop lived in the calling agent's
session, not in our system.** A less persistent client just gets a failed fix. We
cannot claim a reject→iterate→green loop as a product property when it's a property
of whoever happened to be driving.

**No refusal at the reasoning layer.** Refusal exists only at the gate ("I couldn't
prove it"). Nothing says "the evidence for this finding is too thin to act on" *before*
we spend a proof run on it. The CI Fixer's most valuable measured behavior — SAFE_ESCALATE,
0 false ships across every shadow dispatch — has no analogue here.

A raises the floor. B raises the ceiling. Ship A first; B is only measurable against it.

---

## 2. What B is not

**It is not the CI Fixer DAG pointed at a new input.** That DAG cannot accept this work:

- `cifix_techlead` hard-requires `repo, branch, failing_job_id, pr_number`
- 4 of its 12 tools (`fetch_ci_log`, `get_pr_context`, `get_pr_diff`, `get_ci_history`)
  exist only because a failing GitHub CI run is there to read
- `cifix_commander` needs a `WorkOrder` + `Run` + a `CIIntegration` row

An API integration bug has no failing workflow, no PR, no CI log. The DAG is CI-shaped
by construction and has no door for this.

What transfers is the **pattern**, not the code: investigate → adversarially review →
implement → verify, with a bounded loop and an honest refusal. Reimplemented for this
trigger, sharing the provisioner and the sandbox, borrowing nothing that assumes CI.

---

## 3. Architecture

```
   POST /v1/find_bugs | /v1/fix_bug        engine="pipeline"
              │
   ┌──────────▼───────────┐
   │ 1. INVESTIGATOR      │  read-only: Read, Grep, Glob
   │    brain-grounded    │  in:  code + brain's ranked failure classes (from A)
   │                      │  out: findings[] {file:line, class, evidence[], confidence}
   └──────────┬───────────┘  refuses: confidence < threshold → DECLINED, no spend downstream
              │
   ┌──────────▼───────────┐
   │ 2. CHALLENGER        │  clean context — never sees the investigator's reasoning
   │    static rubric     │  in:  finding + code only
   │    default-ACCEPT    │  out: ACCEPT | BLOCK(reason) | WARN
   └──────────┬───────────┘  blocks only on enumerated, evidence-backed objections
              │
   ┌──────────▼───────────┐
   │ 3. IMPLEMENTER       │  Read, Grep, Glob, Edit, Write
   │    minimal diff      │  in:  accepted finding + brain fix_pattern (from A)
   └──────────┬───────────┘  out: unified diff
              │
   ┌──────────▼───────────┐
   │ 4. SELF-VERIFIER     │  reuses synthesize_probe_task AS-IS
   │    probe VIOLATED→   │  authors a probe, independently re-runs it, trusts it ONLY
   │    HELD              │  if it exits 1 on the buggy tree; then runs it on the fixed
   └──────────┬───────────┘  tree and requires exit 0
              │
        pass? ─┴─ no ──► iterate (max 2) with the failure as new evidence ──► DECLINED
              │
             yes
              ▼
   diff + transcript returned to FetchSandbox
              │
   FetchSandbox's prove_fix + curated scenario = the independent gate (unchanged)
```

### Two-tier verification, and why it matters

Tier 1 is B's **self-verifier**: `synthesize_probe_task`, already built and already
honest — it re-runs the synthesized probe itself and discards any probe that doesn't
reproduce on the buggy code. B is allowed to iterate against this.

Tier 2 is **FetchSandbox's curated scenario**, unchanged and authoritative. B never
sees it, never tunes against it, and cannot mark its own homework at this tier.

This is the load-bearing separation. If B could iterate against the scenario that
certifies it, we would be training to the test — and the honest-green gate would stop
meaning anything. The subprocess proposes; the gate disposes; B is allowed to check
its own work with a *different* instrument before proposing.

---

## 4. One route, two engines

`engine: "subprocess" | "pipeline"`, default `"subprocess"`, echoed in the response
next to `grounding`.

Not a second route, and not a second repo. One contract, one auth path, one nginx rule,
one thing to secure. The shadow arm is a request parameter; the kill switch is FetchSandbox
no longer sending it; the cleanup is deleting a branch. If B loses, nothing needs to be
untangled.

```json
{"available": true, "diff": "...", "engine": "pipeline",
 "grounding": {"spec": "paddle", "grounded": true, ...},
 "transcript": {...}}
```

---

## 5. Statelessness — deliberate

B writes **nothing** to postgres. No runs table, no tasks table, no verdict model, no
provenance table.

The per-role trail comes back in the response as a `transcript`, and FetchSandbox records
it in the eval ledger alongside the receipt it already owns. The runtime is not the system
of record for a proof it doesn't own (contract §0), and the CI Fixer's ledger exists for a
different problem — a long-running GitHub-triggered pipeline nobody is watching. B is a
synchronous request/response with a caller who is watching.

```json
"transcript": {
  "engine": "pipeline", "iterations": 2, "cost_usd": 3.41, "seconds": 214,
  "roles": [
    {"role": "investigator", "outcome": "3 findings", "confidence": [0.9, 0.6, 0.3],
     "cost_usd": 1.12},
    {"role": "challenger", "outcome": "1 ACCEPT, 1 BLOCK, 1 WARN",
     "blocked": [{"finding": 2, "reason": "no code path reaches this branch"}],
     "cost_usd": 0.71},
    {"role": "implementer", "outcome": "diff, 1 file", "cost_usd": 0.94},
    {"role": "self_verifier", "outcome": "VIOLATED→HELD", "probe_lang": "python",
     "cost_usd": 0.64}
  ],
  "declined": null
}
```

---

## 6. Refusal

B may return `DECLINED` with a reason, and that is a **success outcome**, not an error:

| Trigger | Why it's right |
| --- | --- |
| Investigator confidence below threshold on every finding | Don't spend a proof run on a guess |
| Challenger BLOCKs and the investigator can't answer the objection | The objection stands |
| Self-verifier can't author a probe that reproduces | We cannot demonstrate the bug is real |
| Iteration cap or cost cap reached | Bounded by construction |

`DECLINED` is distinct from `available: false` (we couldn't run) and from a returned diff.
A declined finding costs a fraction of a full run — refusing early is the cheap path, which
is what makes it credible rather than aspirational.

---

## 7. Cost and caps

A's measured baseline: **$0.87 / $0.47 / $1.03 / $1.14** per proven fix across four specs
(2026-08-04 sweep, $3.51 total). B adds a challenger and up to two iterations, so expect
**3–5×**.

Starting caps — derived from the CI Fixer's proven envelope, scaled to these shorter runs,
and to be tuned by the first sweep rather than trusted now:

| Cap | Value | Basis |
| --- | --- | --- |
| investigator | $1.50 | CI Fixer TL is $5 for a much larger context |
| challenger | $1.00 | 4 turns hard cap; iteration plateaus past 2 rounds on strong bases |
| implementer | $1.00 | matches the CI Fixer engineer's $1 |
| self-verifier | $1.50 | probe synthesis + two runs |
| **per-run** | **$6.00** | hard abort; CI Fixer's analogue is $30 for a 45-min pipeline |
| iterations | 2 | CI Fixer uses 3 for cascading CI failures; this loop is narrower |

Every cap aborts to `DECLINED`, never to a partial answer.

### The model-family question, stated honestly

The CI Fixer's Challenger gets its power from being a *different model family* with clean
context. On this surface, all roles would run on the Max subscription — same family — so
that property is lost, and the cross-family version costs metered tokens.

Start with clean context + a static rubric (free), and **measure whether the challenger is
actually challenging**: track its BLOCK rate. If it approaches 0%, it is theatre and either
gets a different family or gets deleted. Don't assume adversarial review works because it's
in the diagram.

---

## 8. Validation

Shadow, on the frozen corpus. FetchSandbox sends the same request twice —
`engine: "subprocess"` returns to the user, `engine: "pipeline"` is recorded and never
returned. Identical inputs, controlled comparison, zero user-facing risk. `grounding.prompt_sha256`
plus `engine` label the arms.

Corpus: ~12–15 cases, ≥4 specs, stratified — ⅓ easy (A greens today), ⅓ medium (A inconsistent),
⅓ hard (A declines or fixes incompletely).

| Metric | Bar for B |
| --- | --- |
| **False-green rate** | **0. Non-negotiable.** Any false green kills B outright, whatever else it wins. |
| Proven rate, hard tier | must beat A by a margin justifying 3–5× cost |
| Proven rate, easy tier | must not regress below A |
| Challenger BLOCK rate | must be > 0, or the challenger is theatre |
| DECLINED rate | acceptable at any level; wrong answers are not |
| Cost per proven fix | reported, not optimized, in round 1 |

**Kill criterion, written before building:** if B does not beat A on the hard tier by a
margin that justifies its cost, **B is deleted and A stays.** Recorded here so it can't be
relitigated later on the strength of how much work it was.

---

## 9. Failure modes and their bounds

| Risk | Bound |
| --- | --- |
| Cost blowup | Per-role and per-run hard caps; abort to DECLINED |
| Infinite loop | Iteration cap 2, enforced by a counter, not by a prompt |
| Challenger rubber-stamps | BLOCK rate is a tracked metric with a stated failure threshold |
| Investigator invents bugs | Self-verifier must author a probe that reproduces, or DECLINED |
| B tunes itself to the gate | B never sees the curated scenario (§3, tier 2) |
| B destabilizes A | Separate engine branch, default off; A's path is untouched |
| B destabilizes the CI Fixer | Shares only `provisioner.py` + `env_detector.py`, both pure and already shared |

---

## 10. Implementation plan

Prerequisites, in order: **A shipped** (B's investigator is grounded by the same brain
wiring) and **corpus frozen** (otherwise B is unfalsifiable).

| File | Change |
| --- | --- |
| `phalanx/fsproof/__init__.py` | **new package.** Keeps B out of `ci_fixer_v3/`, whose name is already misleading for this surface. |
| `phalanx/fsproof/roles.py` | **new.** Role prompts + the static challenger rubric. |
| `phalanx/fsproof/pipeline.py` | **new.** The loop: investigate → challenge → implement → self-verify, caps, transcript assembly. Never raises. |
| `phalanx/fsproof/caps.py` | **new.** Cost/iteration constants in one place, importable by tests. |
| `phalanx/ci_fixer_v3/find_bugs_task.py` | ~5 lines: branch on `engine`. |
| `phalanx/ci_fixer_v3/fix_bug_task.py` | ~5 lines: branch on `engine`. |
| `phalanx/api/routes/find_bugs.py`, `fix_bug.py` | `engine` request field, `engine` + `transcript` response fields. |
| `tests/unit/fsproof/test_pipeline.py` | **new.** Caps abort, iteration cap, DECLINED paths, challenger BLOCK honored, transcript shape, never-raises. |

Reused as-is, not modified: `synthesize_probe_task`, `run_probe_task`, `provisioner`,
`env_detector`, `grounding`.

Zero changes to `phalanx/agents/`, `phalanx/shadow/`, `alembic/`.

---

## 11. Out of scope

- Multi-container scenarios (contract Gap 3) — not needed until a scenario fails without it
- Service lifecycle / `boot` (contract Gap 2) — same
- Any change to the honest-green gate, the receipt, or the scenario registry — all FetchSandbox's
- Replacing A. B must earn that by beating it on the corpus, and until then A is the default.
