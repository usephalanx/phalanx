# v1.7 Architecture Gaps — Research-Driven Roadmap

**Status**: 2026-05-02. Synthesizes 4 parallel research reports (failure taxonomy + fix shape catalog + misdiagnosis traps + state-of-the-art comparison) against current v1.7 design. **Locks the order of architectural revisions before implementation.**

**Premise**: The locked v1.7 architecture (Commander = TPM, TL = sole LLM, ICs = typists) is correct in shape but incomplete in capability. v1.7 TL handles ~30% of real-world CI failures well; the other ~70% require knowledge or self-critique TL doesn't have today. This doc lists the gaps in priority order and locks the implementation sequence.

**Source reports** (each ~1500-1800 words, full text in conversation transcripts):
- A — Failure Taxonomy (18 OSS PRs across pandas/numpy/pytest/mypy/ruff/black/aiohttp/httpx)
- B — Fix Shape Catalog (25 PRs across Python/Go/Rust/JS/TS/Ruby)
- C — Misdiagnosis Trap Catalog (12 patterns from pytest/numpy/cpython/django)
- D — State of the Art (Claude Code, Devin, Copilot Workspace, Cursor, Aider, Sweep, OpenHands, SWE-agent)

---

## The five gaps, ranked by impact / cost

| # | Gap | What v1.7 TL does today | What frontier does | Impact | Cost |
|---|---|---|---|---|---|
| 1 | **No adversary; first plan = only plan** | Single LLM emits fix, downstream executes | Devin's Critic, Sweep's 3x self-review, SWE-Debate | HIGH — closes Bug #17 class structurally | ~$0.02/run + 2-3 days |
| 2 | **No "this repo's history" awareness** | Cold-starts every run | Aider repo-map PageRank, AutoCodeRover AST | MED-HIGH — same flake re-diagnosed forever | 1 day for git probe; 1 week for repo-map |
| 3 | **No memory of past fixes** | No persistent learning | Reflexion episodic memory, Devin cross-session | MED — narrative win for marketplace | 1 week (per-repo JSONL + BM25) |
| 4 | **TL never sees real sandbox output mid-plan** | SRE runs AFTER plan emitted | SWE-agent ACI, Aider `--auto-test` | MED — kills "fix targets wrong error" class | 1-2 weeks (commander refactor) |
| 5 | **Self-critique catches hallucination, not misdiagnosis** | c1-c7 (file/symbol/line exists, command resolves) | Devin Critic + adversarial review of plan correctness | HIGH — catches 12 trap classes from Agent C | 1 week (subset of c8-c16) |

---

## Implementation order — locked

### Tier 1 — Ship this week (highest leverage, lowest cost)

**Step 1.1 — `git log -S` + grep probe before TL plans** (gap #2 cheap version)
- Before TL invokes its first tool, commander attaches: `git log -S<distinctive_error_token> --since=180d` output + `grep -r <token>` hits as a "we've touched this before" prefix.
- Pure determinism, no LLM, no embeddings.
- Surprisingly high payoff per Agent A research — most CI failures echo prior fixes in the same repo.
- Cost: ~1 day.
- Closes traps #2 (bisect lies) and #4 (env/CI changed last week) partially.

**Step 1.2 — Add c11 (environmental-control) + c12 (isolation-test) self-critique** (gap #5 subset)
- c11: if `failing_command` previously passed and the diff touches no related code, commander runs `git log --since=<last_green> .github/workflows/ Dockerfile* requirements*.txt` and surfaces deltas to TL.
- c12: if `root_cause` names a specific test, commander runs that test in isolation in the sandbox. If it passes alone, treat as test-pollution and demand TL grep for `setup_module`, `autouse=True`, `monkeypatch.setenv`, `os.environ[]`, `sys.modules[]`.
- Both are deterministic — TL invokes a tool, doesn't reason. Cost: ~1 week.
- Closes traps #4-9 from Agent C.

### Tier 2 — Ship next sprint (Challenger architecture)

**Step 2.1 — Challenger LLM pass between TL emit and commander dispatch** (gap #1)
- New role: `cifix_challenger` (Sonnet, single pass, ~$1 budget).
- Input: TL's full fix_spec + ci_log_text + failing_command output + the relevant file contents TL referenced.
- Output: `{accept: bool, objection: str | null, severity: "P0" | "P1" | null}`.
- If `severity == P0` → route back to TL with objection as additional input. Cap at 1 iteration. If second pass also rejected → escalate.
- Architecture stays clean: Challenger does NOT propose plans, only reviews. Engineer remains a typist.
- Cost: ~2-3 days build, +$0.02/run, run cap moves $25 → $30.
- This is the single biggest "Claude-in-IDE bar-raise" we can do per Agent D.

### Tier 3 — Ship in v1.8 (capability extensions)

**Step 3.1 — Episodic memory per repo** (gap #3)
- Per-repo JSONL: `{run_id, failing_command, root_cause, fix_summary, files_modified, outcome}`.
- BM25 lookup at run start; top-3 most similar past fixes attached to TL input.
- ~1 week build. Builds the "Phalanx accumulates wisdom" story for marketplace.

**Step 3.2 — Repo-map structural context** (gap #2 deeper version)
- Tree-sitter parse + PageRank symbol graph (Aider-style, no embeddings).
- Filter by failing-file imports, attach top-N symbols to TL input.
- ~1 week build. Closes the "TL hallucinates function signatures" class.

### Tier 4 — Defer (largest refactors)

**Step 4.1 — Sandbox probe action mid-plan** (gap #4)
- TL can emit `probe` step: "run `pytest path::test -x` in sandbox, return last 50 lines" BEFORE final fix_spec.
- Requires commander state-machine refactor — accept probe results into TL's next turn.
- 1-2 weeks. Defer until #1 + #5 are measured.

**Step 4.2 — Additional self-critique rules c8/c10/c13/c15/c16** (gap #5 remaining)
- c8 plugin-aware, c10 invariant-vs-value, c13 cache-side-effect, c15 time-boundary, c16 cross-process-serialization.
- Lower-volume traps; ship when data shows we hit them.

---

## What each tier closes

| Tier | Failure classes addressed | Bug classes prevented |
|---|---|---|
| 1 | Test pollution; recent infra changes; bisect lies; cache-replay quirks | "wrong test diagnosed", "fix where the symptom is, not the cause" |
| 2 | Confabulation on novel bugs; symptom-only fixes; over-confident on weak evidence | Bug #17 (engineer can't recover from bad spec); Devin's Critic-class catches |
| 3 | Repeated re-diagnosis of same issue; missing "fixed it before" context | "system never learns" |
| 4 | TL writes fix targeting wrong error; structural file context missing | "TL hallucinates signatures"; "fix doesn't compile" |

---

## What stays unchanged (locked architecture)

- **Commander stays deterministic.** No LLM in commander. No multi-agent commander.
- **Engineer stays a typist.** No LLM judgment in engineer. coder_subagent stays deleted.
- **TL stays the SOLE strategic LLM.** Challenger reviews TL's output; doesn't propose alternatives.
- **Cost matrix** ($5 TL / $4 SRE / $1 engineer) unchanged. Run cap moves $25 → $30 to accommodate Challenger.
- **DoD unchanged**: one external Python repo, real CI red → real CI green, fully automated.

---

## Open question — resolved before build

**Q: Should Challenger have access to episodic memory (Tier 3)?**
A: No, not initially. Ship Challenger alone in Tier 2. Add memory access in Tier 3 if data shows Challenger is missing repo-specific context. Sequencing matters — adding both at once mixes signals.

**Q: Which c-checks ship first (c11/c12 vs c14)?**
A: c11 + c12 in Tier 1; c14 (template-literal-guard, already shipped as Bug #14 fix) gets codified as a self-critique check in the same Tier-1 sprint.

**Q: Does Challenger run in parallel with verify, or before?**
A: Before. Sequence: TL emit → commander persists tasks → Challenger reviews TL's output → if accepted, commander dispatches engineer/SRE. Challenger blocks dispatch on P0 objection.

---

## What this means for the v1.7 spec doc

[docs/v17-tl-as-planner.md](docs/v17-tl-as-planner.md) needs an addendum (not a rewrite):
- §2 (Roles + LLM access) gains a "Challenger" row in the table
- §3 (Architecture flow) gains the Challenger box in the diagram
- §4 (TL contract) unchanged — TL output schema doesn't change
- §6 (Engineer contract) unchanged
- §10 (Phase breakdown) gains Tier 1.1, 1.2, 2.1 as Phase 1 sub-phases

I will draft the addendum after Tier 1 ships and the Challenger design is locked.

---

## What this is NOT

This roadmap does NOT propose:
- Splitting TL into 3 LLM agents (planner / coder / patch-author) — already rejected; conflicts with the locked architecture
- Multi-agent debate between two TLs — overkill for our scale; Challenger is enough
- Embedding-based RAG — Aider's tree-sitter PageRank is cheaper and works for our scale
- LLM in commander — explicit non-goal

These would all be valid v2.0 conversations, not v1.7.
