"""v1.7 TL output corpus — fixtures + invariant harness.

Each fixture in this directory describes a CI failure shape sourced from
real OSS bugs (humanize a47a89e, etc.). The harness runs TL (real or
mocked) against each fixture and verifies the output satisfies the
fixture's invariants — e.g., plan_validator passes, task_plan emits
the right tasks for ICs and SRE, root_cause references the actual error.

Two test tracks share the corpus:
  - Tier-1 (this directory's _tests/): canned TL outputs verify the
    invariant logic itself (no LLM calls, fast, deterministic).
  - Tier-2 (separate harness): real GPT-5.4 invocations against the
    fixtures, validating that the v1.7 TL prompt produces v1.7-shaped
    output. Gated by env var so CI doesn't burn dollars.

Fixture shape — see CorpusFixture in `_types.py`. Each fixture is a
Python module with module-level constants: `FIXTURE = CorpusFixture(...)`.

Curated shapes (initial corpus):
  - 01_lint_e501_simple — single-line Ruff E501; no SRE setup needed
  - 02_importerror_missing_dep — pip dep missing; SRE adds package
  - 03_humanize_tz_cascading — real bug a47a89e; TL anticipates cascade
  - 04_pytest_delete_test_exit_4 — Bug #16 shape; verify needs broad scope
  - 05_gha_workflow_escalate — env-mismatch escalate (don't touch .github/)
  - 06_assertion_logic_fix — typical test_fail with code-only fix

Add more by following the same template; harness picks up new files
automatically via `discover_corpus()`.
"""
