# Phase 2 Validation Report — v1.7.2.3 on internal testbed

**Date**: 2026-05-03
**Stack version**: v1.7.2.3 (deployed at 15:00 UTC, lint cell triggered at 15:19 UTC)
**Testbed**: `usephalanx/phalanx-ci-fixer-testbed`
**Status**: 4/4 SHIPPED by Phalanx internal verdict — but with **two important caveats** the user must read before approving Phase 3.

---

## TL;DR

| Cell | Phalanx verdict | Iterations | GitHub Lint | GitHub Test+Coverage | Honest grade |
|---|---|---|---|---|---|
| lint | SHIPPED ✅ | 1 | success | success | ✅ **True green** |
| test_fail | SHIPPED ✅ | 1 | success | success | ✅ **True green** |
| flake | SHIPPED ✅ | 1 | **failure** | success | ⚠️ **Partial** — TL's targeted job is green, but engineer's edit broke an unrelated Lint check |
| coverage | SHIPPED ✅ | 1 | success | **failure** | ❌ **False ship** — TL diagnosed the wrong failing job; coverage drop is still red |

**No replan loops. All 4 commit_shas match across engineer + verified. v1.7.2.3 closed the workspace-sync bug.**

But: the system shipped 2 commits that GitHub's full CI doesn't agree are fixed. This is a **measurement gap** in Commander's ship decision — it trusts SRE Verify's narrow check without re-confirming the full failing job. Critical for the humanize launch.

---

## 1. lint cell — TRUE GREEN ✅

| Field | Value |
|---|---|
| **run_id** | `1826ee16-7bff-404e-8f2a-3136b5d3e9b9` |
| **PR** | usephalanx/phalanx-ci-fixer-testbed#35 |
| **Failing CI check** | `Lint` (job_id 74122988656) |
| **Failing command** | `ruff check .` |
| **TL hypothesis** | *"The PR added verbose_description() with a 129-character string literal on one line in src/calc/formatting.py, causing Ruff E501 line-length lint failure."* |
| **TL confidence** | 0.94 |
| **TL verify_command** | `ruff check src/calc/formatting.py` |
| **TL verify_success** | `{"exit_codes": [0]}` |
| **Engineer commit_sha** | `d275234e11327b49282ee8cbe94fbd176fcce791` |
| **Verified commit_sha** | `d275234e11327b49282ee8cbe94fbd176fcce791` |
| **SHA match** | ✅ identical |
| **Sandbox sync method** | `git_fetch_reset_hard` |
| **Verify exit_code** | `0` |
| **Verify stdout** | `"All checks passed!"` |
| **Verify stderr** | `""` |
| **Failure fingerprint** | `df89f65f9c74cc67` (first iteration only — no comparison needed) |
| **Final status** | SHIPPED |
| **Replan loop?** | **No** — single iteration |
| **GitHub CI on head** | Lint=success, Test+Coverage=success |

**Assessment**: Clean fix. TL diagnosed correctly, engineer wrapped the line, narrow verify confirmed, GitHub agreed.

---

## 2. test_fail cell — TRUE GREEN ✅

| Field | Value |
|---|---|
| **run_id** | `d6ea2fcb-c89b-47c8-b165-7c8248f5caca` |
| **PR** | usephalanx/phalanx-ci-fixer-testbed#36 |
| **Failing CI check** | `Test + Coverage` (job_id 74123406764) |
| **Failing command** | `pytest --cov=src/calc --cov-fail-under=80 --timeout=2` |
| **TL hypothesis** | *"The PR changed src/calc/math_ops.py so multiply() returns a + b instead of a * b, breaking test_multiply."* |
| **TL verify_command** | `pytest tests/test_math_ops.py::test_multiply -xvs` (narrow to the failing test) |
| **Engineer commit_sha** | `53cff1a5be756db32baaf3a26b396dcc499c55a3` |
| **Verified commit_sha** | `53cff1a5be756db32baaf3a26b396dcc499c55a3` |
| **SHA match** | ✅ identical |
| **Sandbox sync method** | `git_fetch_reset_hard` |
| **Verify exit_code** | `0` |
| **Verify stdout** | `". [100%] 1 passed in 0.02s"` |
| **Verify stderr** | `""` |
| **Failure fingerprint** | `7d0ade76e38f89da` |
| **Final status** | SHIPPED |
| **Replan loop?** | **No** — single iteration |
| **GitHub CI on head** | Lint=success, Test+Coverage=success |

**Assessment**: Clean fix. TL targeted the specific failing test, engineer reverted the bug, narrow verify confirmed, GitHub agreed.

---

## 3. flake cell — PARTIAL ⚠️

| Field | Value |
|---|---|
| **run_id** | `4003a6b6-378a-4132-b91a-15e832d7868d` |
| **PR** | usephalanx/phalanx-ci-fixer-testbed#37 |
| **Failing CI check** | `Test + Coverage` (job_id 74123724416) |
| **Failing command** | `pytest --cov=src/calc --cov-fail-under=80 --timeout=2` |
| **TL hypothesis** | *"PR #37 added an intentionally flaky test that sleeps to trip pytest's --timeout=2 in roughly 1 of N runs."* |
| **TL verify_command** | `pytest tests/test_math_ops.py --timeout=2 -q` |
| **Engineer commit_sha** | `5dd28ac8aa747ea3899b54f361996a2e04736880` |
| **Verified commit_sha** | `5dd28ac8aa747ea3899b54f361996a2e04736880` |
| **SHA match** | ✅ identical |
| **Sandbox sync method** | `git_fetch_reset_hard` |
| **Verify exit_code** | `0` |
| **Verify stdout** | `"..... [100%]"` (5 tests passed, no flake observed in this run) |
| **Verify stderr** | `""` |
| **Failure fingerprint** | `e94c8d426291f74a` |
| **Final status** | SHIPPED |
| **Replan loop?** | **No** — single iteration |
| **GitHub CI on head** | **Lint=failure**, Test+Coverage=success |

**Assessment**: ⚠️ TL fixed the targeted flake (the failing job we asked about), but the engineer's edit broke an unrelated Lint check on the same head commit. Phalanx considered the run shipped because SRE Verify only ran the narrow `pytest` command. We didn't re-run lint, so the broken-lint side-effect was never caught internally.

**Two interpretations**:
- *Optimistic*: the original failing job (Test+Coverage) is now green. From the customer's "did Phalanx fix my failing CI" perspective, yes.
- *Pessimistic*: the customer's PR is now red on a *different* check that was green before Phalanx touched it. This is a regression Phalanx introduced.

---

## 4. coverage cell — FALSE SHIP ❌

| Field | Value |
|---|---|
| **run_id** | `d9005ec6-7206-4764-8384-0570a6c39983` |
| **PR** | usephalanx/phalanx-ci-fixer-testbed#38 |
| **Failing CI check** | `Test + Coverage` (job_id 74123989310) — both Lint and Test+Coverage failed pre-fix |
| **Failing command (per ci_context)** | `pytest --cov=src/calc --cov-fail-under=80 --timeout=2` |
| **TL hypothesis** | *"The lint job failed because PR #38 added code in src/calc/math_ops.py that doesn't match Ruff's preferred formatting (multiple consecutive blank lines)."* |
| **TL verify_command** | `ruff format --check src/calc/math_ops.py` ⚠️ **wrong target** |
| **Engineer commit_sha** | `b8dc1bb7553e2c212b32922866f7cc1a8198fd43` |
| **Verified commit_sha** | `b8dc1bb7553e2c212b32922866f7cc1a8198fd43` |
| **SHA match** | ✅ identical |
| **Sandbox sync method** | `git_fetch_reset_hard` |
| **Verify exit_code** | `0` |
| **Verify stdout** | `"1 file already formatted"` |
| **Verify stderr** | `""` |
| **Failure fingerprint** | `eefeb1366b818d13` |
| **Final status** | SHIPPED |
| **Replan loop?** | **No** — single iteration |
| **GitHub CI on head** | Lint=success, **Test+Coverage=failure** |

**Assessment**: ❌ TL diagnosed the *secondary* failing check (lint formatting) instead of the *primary* one passed in `failing_job_name="Test + Coverage"`. The patch added uncovered functions (`percentage`, `average`) without tests, causing both formatting AND coverage failures. TL fixed formatting, ignored coverage. SRE Verify ran only TL's chosen command (`ruff format`), got exit 0, declared all_green. Commander shipped.

The original `failing_command` (`pytest --cov=src/calc --cov-fail-under=80`) is **still red on GitHub** because the new functions still have no tests.

---

## 5. What changed v1.7.2.2 → v1.7.2.3 — and why the loop stopped

### The bug v1.7.2.2 left exposed

Yesterday's lint cell run (`f5ffd0c8`) reached **iter-3 turn-cap** with TL emitting confidence 0.4 and the diagnosis:

> *"The remaining reported failure is a stale Ruff E501 against an older one-line version of src/calc/formatting.py; the current workspace already contains the wrapped-string fix."*

TL had figured out the real bug — the sandbox was testing **stale content**.

### Root cause

`provisioner._docker_cp_workspace` does a one-shot `docker cp <workspace>/. <container>:/workspace` at SRE setup time. Engineer's later edits land on the host (`/tmp/forge-repos/...`) but **never propagate into the sandbox**. So when SRE Verify ran `ruff check src/calc/formatting.py` via `docker exec`, it was reading the original unwrapped line — even though engineer had pushed a correctly-wrapped commit to GitHub.

The narrow verify (v1.7.2.2) made the bug *more visible* — it was now running TL's exact command, but against the wrong filesystem state.

### Fix in v1.7.2.3

**Commit `1c15cc0`** added `_sync_sandbox_to_commit` to `cifix_sre.py`. Before running `verify_command`, SRE Verify now:

1. Reads the latest engineer commit_sha from upstream task output
2. Runs in the sandbox: `git config --global --add safe.directory /workspace && (git fetch origin <sha> --depth=1 || git fetch origin <branch> --depth=20) && git reset --hard <sha> && git rev-parse HEAD`
3. Records the resulting HEAD as `verified_commit_sha`
4. Returns `sandbox_sync.method = "git_fetch_reset_hard"` and `sandbox_sync.ok = (verified_commit_sha == target_sha)`

This makes **git** the source of truth, not the docker-cp snapshot. Branch HEAD is intentionally NOT used (branches can move during a run via concurrent push or force-push) — only the exact engineer commit_sha is safe.

### Proof it ran on every cell

All 4 sre_verify rows show `"sandbox_sync": {"method": "git_fetch_reset_hard"}`. All 4 show `verified_commit_sha == engineer_commit_sha`. This is the deterministic gate that closes the bug: if the sandbox can't reach the exact engineer sha, verify fails fast and Commander rejects the run.

### Other v1.7.2.3 fixes that backstopped the gate

- **Fix 1 (stdout capture)**: Verify now records both stdout and stderr. Pre-fix, ruff failures showed `stderr=""` and TL had no signal. Post-fix, lint cell shows `"All checks passed!"` in stdout — the verifying agent has clear evidence on success too.
- **Fix 3 (failure fingerprint)**: Each verify task now writes a 16-char hash. Commander has `_collect_verify_fingerprints` + `is_repeated` to detect "same failure twice in a row → stop iterating." Hasn't fired in any of the 4 runs (no second iteration on any of them) but the gate is in place.
- **Fix 4 (sha-mismatch gate)**: Commander rejects an `all_green` verdict if `verified_commit_sha != engineer_commit_sha`. Hasn't fired in any run (all 4 match). Defense in depth.
- **Fix 5 (patch safety)**: Engineer step interpreter blocks edits to `.github/workflows/`, `.codecov.yml`, etc. and rejects test-deletion / `@pytest.skip` injection. Not exercised in any of these 4 cells (TL didn't try to do anything unsafe).
- **Fix 7 (escalation record)**: Stores structured ledger to `runs.error_context` on FAIL. Not triggered (all SHIPPED).

### Why the loop stopped (concretely)

Compare yesterday's lint run vs. today's:

| Aspect | v1.7.2.2 (yesterday) | v1.7.2.3 (today) |
|---|---|---|
| Iterations | 3 (turn cap) | **1** |
| Engineer commits pushed | 2 (4283033, a432e3c) | 1 (d275234e) |
| Sandbox content reflects engineer's push | ❌ no — frozen at SRE setup | ✅ yes — git reset --hard |
| Narrow verify_command run? | ✅ yes (v1.7.2.2 fix) | ✅ yes |
| Verify saw current code? | ❌ no — stale | ✅ yes |
| Verify stdout captured? | ❌ no (Fix 1 missing) | ✅ yes — `"All checks passed!"` |
| Final state | turn_cap_reached / FAILED | **SHIPPED** |

The loop stopped because the verifier finally agrees with the engineer's committed code. There's nothing to replan against — the patch was always correct; only the verification was lying.

---

## 6. The two caveats — implications for humanize Path 1

**Phalanx-internal: 4/4 SHIPPED.** GitHub-truth: **2/4 fully green, 1/4 partial regression, 1/4 false ship.**

The architectural gap: Commander accepts SRE Verify's verdict on the *narrow verify_command*. It doesn't independently confirm:
1. **All originally-failing CI checks** now pass on the engineer's pushed commit (full-CI re-run, not just narrow command)
2. **No previously-green checks** went red as a side effect of the patch (regression detection)

For internal testbed cells this is mostly fine — the cells are designed to fail one specific check. For **humanize** (real OSS repo), this matters more:

- humanize Path 1 has **multiple CI jobs** (test matrix across Python versions, lint, mypy, etc.). TL will pick *one* failing one to diagnose. If others are also failing, the SHIP signal is misleading.
- A real-world fix that breaks an unrelated check (the flake-cell shape) burns trust on the marketplace launch.
- A real-world fix that targets the wrong failing job entirely (the coverage-cell shape) is a public proof-point disaster.

### Recommended action before Phase 3

**Two options**:

- **(a) Add a "full-CI re-confirm" gate to Commander** — after SRE Verify reports `all_green`, poll GitHub's check-runs API for the engineer's head sha; only finalize SHIP if **every** check that was previously failing is now success AND no previously-green check turned red. ETA: 1-2 hours.

- **(b) Run humanize anyway** — accept the gap as known. If humanize Path 1 ships green AND GitHub agrees, ship. If GitHub disagrees, build (a) before relaunching.

My read: **(a) is the right call before humanize.** The alternative is a 50/50 chance the marketplace proof point is a false ship that we discover only after sharing publicly. 1-2 hours is cheap insurance.

But your call — the gap is real and the data is here for you to weigh.

---

## 7. Repo pointers

- v1.7.2.3 commits: `1c15cc0`, `e919f11`, `fdb8cd4`, `a99b0ce`
- Tag: `v1.7.2.3` (pushed)
- Audit doc (pre-v1.7.2.3): `docs/audit-2026-05-03-cifixer-v3.md`
- This report: `docs/phase2-validation-report-2026-05-03.md`
- Commander gates: `phalanx/agents/cifix_commander.py:_collect_verify_fingerprints`, `_build_and_persist_escalation`, sha-mismatch + runtime + no-progress branches
- SRE sync: `phalanx/agents/cifix_sre.py:_sync_sandbox_to_commit`
- Failure fingerprint: `phalanx/agents/_failure_fingerprint.py`
- Patch safety: `phalanx/agents/_patch_safety.py`
- Escalation record: `phalanx/agents/_escalation_record.py`

End of report.
