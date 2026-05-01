# CI Fixer v3 — Agent contracts (v1.5.0 design)

**Status**: design (2026-05-01). Replaces the implicit-verify pattern with explicit contracts + per-agent self-correction.
**Surfaced by**: humanize test_fail canary 2026-04-30, run `64fefedf-7dd4-4518-850f-0aa5cea2fdad`. TL diagnosed correctly (delete the broken test); engineer applied correctly; engineer's verify ran the original failing_command (a pytest selector for the now-deleted test) → exit 4 → engineer mistook "no tests collected" for failure → no commit. Bug #16 in our retro.
**Two architectural insights driving this** (Raj 2026-05-01):
1. TL's job is incomplete without "how to verify". A senior engineer writing a ticket specifies acceptance criteria; our TL doesn't.
2. Every agent must self-correct before declaring done/failed — Claude/Cursor pattern.

This doc unifies both into a single contract refactor.

## 1. Problem

Today's TL → Engineer contract:

```
TL output:
  root_cause:       str
  fix_spec:         str
  affected_files:   [str]
  failing_command:  str         # what was failing in CI
  confidence:       float
  open_questions:   [str]
```

Engineer infers "re-run failing_command, expect exit 0". Works for 80% of fixes. Breaks when:

| fix type | why exit-0-on-failing_command breaks |
|---|---|
| Delete a broken test | failing_command was `pytest path::test_name`; test now doesn't exist → exit 4 (no tests collected) → mistaken for failure |
| Rename a function/test | failing_command targets old name; exit 4 same way |
| Fix env config (yaml/toml) | failing_command was a CI step; verify needs different shape (e.g., `yq .key < cfg`) |
| Bump library version | failing_command was the test command; what really matters is `python -c 'import X; X.version'` |
| Move a file | failing_command path is now wrong |

Plus: **no agent self-corrects**. TL emits fix_spec with no review pass. Engineer marks "verify failed" without asking "could this exit code mean something else given my fix's intent". Single-shot agents are brittle.

## 2. Goals

1. **Verify-criteria become first-class** — TL specifies how to verify; engineer follows the contract; no implicit fallbacks except for backwards compatibility.
2. **Each agent self-corrects** — TL re-reads its own diagnosis before emitting; engineer re-checks "did my fix actually do what TL asked, given exit code semantics" before declaring failure.
3. **Backwards compatible** — old fix_spec without verify_command still works (engineer falls back to current behavior).
4. **Bug #16 disappears** — "delete test" / "rename" / "config" fixes work without bandaid exit-code matchers.

## 3. Non-goals

- Replacing the agent loop architecture (still Sonnet for engineer, GPT-5.4 for TL).
- Cross-agent retry orchestration beyond what commander already does.
- Self-critique that makes a SECOND fix attempt (one critique pass per agent — if it fails self-check, it surfaces the doubt to the next agent or escalates).

## 4. New TL output schema

```python
fix_spec = {
    # Existing fields
    "root_cause": str,
    "fix_spec": str,
    "affected_files": list[str],
    "failing_command": str,        # what was failing in CI (kept for context)
    "confidence": float,
    "open_questions": list[str],

    # NEW — explicit verify contract
    "verify_command": str,
    """The exact shell command engineer should run after applying the patch
    to confirm success. Often the same as failing_command, but explicitly
    different for delete/rename/config fixes."""

    "verify_success": {
        "exit_codes": list[int],   # acceptable exit codes (default [0])
        "stdout_contains": str | None,
        "stderr_excludes": str | None,
    },
    """How to interpret the verify_command's result. For 'delete test' fixes,
    exit_codes might be [0, 4, 5] (pytest's "no tests collected" is success).
    For 'rename function' fixes where the old test is gone, similar.
    For most code fixes, the default exit_codes=[0] holds."""

    # NEW — self-critique evidence
    "self_critique": {
        "ci_log_addresses_root_cause": bool,
        "affected_files_exist_in_repo": bool,
        "verify_command_will_distinguish_success": bool,
        "notes": str,
    } | None,
    """Optional but encouraged. Result of TL's self-check before emitting.
    Commander/observability uses this to flag low-rigor diagnoses.
    Empty/None = TL didn't run a self-check (older runs, or LLM forgot)."""
}
```

## 5. TL prompt updates

Two new sections in TL system prompt:

**Section A — explicit verify contract**:
```
Every fix_spec MUST include:
  verify_command: the exact shell command the engineer will run after
                  applying your patch to confirm the fix worked.
  verify_success: { exit_codes: [...], stdout_contains: ..., stderr_excludes: ... }

How to choose verify_command:
  - DEFAULT (most code fixes): use the failing_command unchanged.
  - FIX REMOVES A TEST: verify_command should target the WHOLE suite or
    the parent module, not the specific test name. Set exit_codes [0]
    only — broken test is gone, others must still pass.
    Example: verify_command="pytest tests/" if the original was
    "pytest tests/foo.py::test_bar".
  - FIX RENAMES OR MOVES: verify_command targets the new location,
    not the old failing_command path.
  - FIX IS ENV/CONFIG-ONLY: verify_command exercises the config path
    (e.g., `yq .key < cfg.yml` or `python -c 'import X'`).
  - FIX IS A LIBRARY VERSION BUMP: verify_command imports + version-checks.

How to choose verify_success.exit_codes:
  - DEFAULT: [0]
  - FIX DELETES A TEST and verify_command targets only that test:
    [0, 4, 5] (pytest's "no tests collected" = success here).
  - FIX TURNS A FAILING ASSERT INTO A SKIP: [0, 5] depending on how
    pytest reports skips.

How to choose stdout/stderr matchers:
  - Use sparingly. Only when exit code alone is ambiguous.
  - stdout_contains: e.g., "All checks passed" for ruff.
  - stderr_excludes: e.g., "DeprecationWarning" if the fix removes a deprecated call.
```

**Section B — self-critique pass**:
```
BEFORE emitting your final fix_spec via emit_fix_spec, you MUST call
self_critique once with:
  ci_log_addresses_root_cause: did your root_cause match the actual error
                                in the CI log you fetched?
  affected_files_exist_in_repo: have you confirmed (via read_file or glob)
                                that each path you list is real?
  verify_command_will_distinguish_success: walk through what happens when
                                            verify_command runs against the
                                            patched repo. Does exit code
                                            actually flip from non-zero to
                                            in-range? Or does verify_success
                                            need stdout/stderr matchers?
  notes: one sentence — what would a senior engineer flag in your fix_spec?

If any check is False, do NOT emit fix_spec yet. Iterate (re-read CI log,
re-glob, etc.) and re-run self_critique. After 2 iterations max, if you
still can't say all True, set confidence ≤ 0.5 and emit anyway with
open_questions filled in.
```

A new TL tool `self_critique` validates the inputs (must be called once before `emit_fix_spec`).

## 6. Engineer contract changes

**Reading**:
```python
verify_command = fix_spec.get("verify_command") or fix_spec.get("failing_command")
verify_success = fix_spec.get("verify_success") or {"exit_codes": [0]}
```

Backwards-compatible: old fix_spec without `verify_command` falls back to today's behavior.

**Verify success matcher** (replaces "exit_code == 0" check):
```python
def is_verify_success(result, criteria):
    if result.exit_code not in criteria.get("exit_codes", [0]):
        return False, f"exit {result.exit_code} not in allowed {criteria['exit_codes']}"
    needle = criteria.get("stdout_contains")
    if needle and needle not in result.stdout:
        return False, f"stdout missing required substring {needle!r}"
    excl = criteria.get("stderr_excludes")
    if excl and excl in result.stderr:
        return False, f"stderr contains forbidden substring {excl!r}"
    return True, "matches verify_success criteria"
```

**Self-critique pass** (engineer's coder subagent):
Before declaring "verify failed", call a `self_critique_verify` tool with:
- diff_applied: the patch that just landed
- verify_result: exit code + stdout/stderr tails
- tl_verify_success: the criteria TL specified
- ask_self: "Given the fix's intent, does this exit code + output actually mean success or failure?"

If self_critique_verify returns "this is success despite my initial read", flip the verdict and proceed to commit. Else escalate to commander with the ambiguity.

## 7. SRE setup self-critique

SRE setup loop (Phase 1 of agentic SRE) gains an analogous step before report_ready:
- Re-list workflow `uses:` actions
- Re-list capabilities_installed
- Ask: "Did I install everything upstream CI installs? Are there `uses:` actions I didn't recognize?"

If gaps remain → install or report_partial. If unsure → install one more, re-check.

## 8. Backwards compatibility

| field | old fix_spec | new fix_spec | behavior |
|---|---|---|---|
| verify_command | absent | present | engineer uses verify_command if present, else failing_command (today's behavior) |
| verify_success | absent | present | engineer uses criteria if present, else exit_code==0 (today's behavior) |
| self_critique | absent | present | logged for observability; not load-bearing |

Old runs (before v1.5.0) keep working. New runs benefit from the richer contract.

## 9. Test strategy

Tier-1:
- TL prompt's self_critique tool refuses to emit fix_spec if not all True
- Engineer's verify success matcher: exit_code allowed list, stdout_contains, stderr_excludes (each in isolation + combined)
- Engineer's self_critique_verify: scripted scenarios (delete-test exit 4 → flips to success; real test failure exit 1 → stays failed)

Tier-2 / canary:
- Internal testbed all 4 cells SHIP (regression check — verify_command should match failing_command for these and behavior unchanged)
- Humanize test_fail SHIPS (TL produces verify_command="pytest tests/" with [0] OR exit_codes=[0,4,5])
- Humanize lint SHIPS (regression — should still work with explicit verify)

Success criteria:
- 4/4 internal Python no regression
- humanize test_fail SHIPS in single iter
- TL self_critique fields present in ≥ 90% of new runs (observability check)
- Engineer doesn't false-fail any "delete/rename" fix

## 10. Execution plan

| phase | scope | est |
|---|---|---|
| A | TL output schema + parser update + tier-1 tests | 1h |
| B | TL prompt rewrite (sections A + B + examples) + tier-1 prompt-shape tests | 1h |
| C | Engineer reads verify_command/verify_success + matcher + tier-1 tests | 1h |
| D | TL self_critique tool + engineer self_critique_verify tool + tests | 1.5h |
| E | Deploy v1.5.0 + canary (testbed 4/4 + humanize test_fail) | 1h |
| F | Memory + changelog + push | 30m |

**Total: ~6h**. Implement piece by piece with green-bar checkpoints.

## 11. What this doesn't fix

- "Pure infrastructure" failures where no code change is appropriate. SRE charter still owns those.
- Multi-step fixes spanning multiple files where verify needs orchestration. v1.5.0 covers single-verify-command fixes; multi-step verification deferred to v1.6+.
- Race conditions in CI that can't be reproduced in sandbox. Out of scope for verify contract — fundamental sandbox limitation.

## 12. Open questions

1. Should `verify_command` be required, or optional with backwards-compat default? Recommend: optional in v1.5.0 (lower-risk rollout); flip to required in v1.5.1 once observability shows TL emits it consistently.
2. Should self_critique results be persisted in Task.output for retro analysis? Recommend yes — small JSON, useful for debugging.
3. Should commander act on low self_critique confidence (skip engineer dispatch)? Defer; observability first, then policy.
