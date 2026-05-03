"""v1.7 shared schema types — TaskSpec, Step, EnvRequirements, etc.

These types describe the contract between TL (the planner) and the rest
of the system (commander, engineer, SRE). They are TypedDicts so they
serialize as plain JSON in Task.output / Task.description without any
pickling shenanigans.

See docs/v17-tl-as-planner.md §4.2 for the canonical reference.
"""

from __future__ import annotations

from typing import Literal, TypedDict

# Closed agent registry — commander's plan validator rejects any agent
# name not in this set. New agents must be added here AND wired up in
# commander.dispatch + queue routing before TL can name them in plans.
V17_AGENT_REGISTRY: frozenset[str] = frozenset(
    {
        "cifix_sre_setup",   # agentic env-deliverer (v1.7 split from cifix_sre)
        "cifix_engineer",    # step interpreter (no LLM judgment in v1.7)
        "cifix_sre_verify",  # deterministic full-CI verifier
    }
)

V17AgentName = Literal["cifix_sre_setup", "cifix_engineer", "cifix_sre_verify"]


class VerifySuccess(TypedDict, total=False):
    """v1.5 contract — how to interpret a verify command's result."""

    exit_codes: list[int]            # default [0]
    stdout_contains: str | None
    stderr_excludes: str | None


class NarrowVerify(TypedDict):
    """Per-subtask verification (engineer's "did MY change work" gate).

    Distinct from the run's full verify_command which exercises everything.
    """

    command: str
    success: VerifySuccess


class Step(TypedDict, total=False):
    """One executable instruction the engineer (or sre_verify) follows.

    Most fields are optional because each `action` uses a different
    subset. Validators check action-specific required-fields.
    """

    id: int
    action: Literal[
        "read",
        "replace",
        "insert",
        "delete_lines",
        "apply_diff",
        "run",
        "commit",
        "push",
    ]
    file: str | None
    line: int | None
    end_line: int | None              # for delete_lines (inclusive)
    after_line: int | None            # for insert
    old: str | None                   # for replace — exact match required
    new: str | None                   # for replace
    content: str | None               # for insert
    diff: str | None                  # for apply_diff (unified diff format)
    command: str | None               # for run
    expect_exit: int | None           # for run (default 0)
    expect_stdout_contains: str | None
    message: str | None               # for commit
    purpose: str | None               # human-readable, optional


class EnvRequirements(TypedDict, total=False):
    """What env SRE-setup must deliver before engineer/sre_verify run.

    `services` are docker-compose-able single-instance services.
    `reproduce_command` lets SRE deterministically validate it has
    actually delivered an env where the original bug reproduces.
    """

    python: str | None                # e.g., "3.11"
    os_packages: list[str]            # apt/brew package names
    python_packages: list[str]        # pip install candidates
    env_vars: dict[str, str]          # name → value
    services: list[Literal["postgres", "redis", "mysql"]]
    reproduce_command: str            # shell command SRE runs to validate
    reproduce_expected: str           # human-readable expected outcome


class TaskSpec(TypedDict, total=False):
    """One node in TL's task_plan DAG. Commander persists this verbatim
    into Task.description so the executing agent can read it back.

    `task_id` is TL-assigned (T1, T2, ...) — distinct from the DB Task.id
    UUID. `depends_on` is a list of OTHER task_ids that must be in
    terminal-success state before this task is dispatched.
    """

    task_id: str                       # "T2", "T3", ... (TL-assigned)
    agent: V17AgentName
    depends_on: list[str]              # task_ids that must finish first
    purpose: str                       # one-line human-readable
    steps: list[Step]                  # for engineer / sre_verify
    env_requirements: EnvRequirements  # for sre_setup
    narrow_verify: NarrowVerify | None # for engineer's per-subtask gate


class TLOutput(TypedDict, total=False):
    """Full schema TL emits in plan / review / replan modes.

    Mode-dependent fields:
      PLAN    — task_plan REQUIRED; review_decision MUST be null
      REVIEW  — review_decision REQUIRED; task_plan optional (only if REPLAN)
      REPLAN  — task_plan REQUIRED (delta only); replan_reason REQUIRED
    """

    # v1.5 fields (carry-forward, still required in PLAN mode)
    root_cause: str
    fix_spec: str                      # human-readable summary
    affected_files: list[str]
    failing_command: str
    confidence: float
    open_questions: list[str]
    verify_command: str
    verify_success: VerifySuccess
    self_critique: dict                # v1.6 deterministic validator output

    # v1.7 NEW
    task_plan: list[TaskSpec]          # required in PLAN + REPLAN
    env_requirements: EnvRequirements  # for the run as a whole; consumed by SRE setup
    review_decision: Literal["SHIP", "REPLAN", "ESCALATE"] | None
    replan_reason: str | None


# Mode constants — used by commander when constructing TL task input,
# and by TL to branch its system prompt.
TL_MODE_PLAN = "plan"
TL_MODE_REVIEW = "review"
TL_MODE_REPLAN = "replan"

V17_TL_MODES: frozenset[str] = frozenset({TL_MODE_PLAN, TL_MODE_REVIEW, TL_MODE_REPLAN})


class PlanValidationError(ValueError):
    """Raised by `_plan_validator.validate_plan` when TL's task_plan is
    malformed. Commander catches this and marks the TL task FAILED with
    `plan_validation_failed:<reason>`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ─── v1.7.0 Challenger contract ──────────────────────────────────────────────
#
# Adversarial reviewer (Sonnet 4.6) that sits between TL and engineer dispatch.
# Reviews TL's fix_spec; emits structured verdict. Cross-model from TL (GPT-5.4)
# to mitigate self-enhancement bias (Panickssery 2024). Default: ACCEPT.
# Reject only with cited evidence on enumerated failure categories.
#
# See docs/v17-architecture-gaps.md for design rationale.

ChallengerSeverity = Literal["P0", "P1", None]
ChallengerVerdictKind = Literal["accept", "block", "warn"]

# Closed taxonomy of objection categories. Mirrors the trap catalog in
# docs/v17-architecture-gaps.md (Agent C report).
ChallengerObjectionCategory = Literal[
    "verify_command_does_not_retrigger_failure",  # verify_command won't actually exercise the failing check
    "verify_success_too_loose",                    # exit_codes/stdout_contains too permissive
    "fix_targets_symptom_not_root_cause",          # the diff papers over the failure
    "ungrounded_step",                             # step references file/line TL didn't read
    "stale_old_text",                              # replace step's `old` doesn't appear in target file
    "affected_files_mismatch",                     # plan files don't match the actual error location
    "missing_env_dependency",                      # verify_command needs a pkg env_requirements doesn't list
    "edits_ci_infrastructure",                     # plan touches .github/ etc when it should escalate
    "misdiagnosis_test_pollution",                 # diagnosis blames a test that's actually a victim of pollution
    "misdiagnosis_env_drift",                      # diagnosis blames code; failure is recent infra change
    "low_confidence_high_stakes",                  # confidence ≤ 0.5 but plan still ships destructive changes
    "other",                                        # catch-all; must be backed by specific evidence
]


class ChallengerObjection(TypedDict):
    """One specific concern raised by the Challenger.

    Every objection MUST cite evidence — a quoted line from the fix_spec,
    ci_log, or repo file. Reject without evidence is treated as sycophantic
    boilerplate and downgraded to a warn.
    """

    category: ChallengerObjectionCategory
    severity: ChallengerSeverity
    claim: str            # one-sentence assertion of what's wrong
    evidence: str         # quoted excerpt from fix_spec/log/file (REQUIRED)
    suggestion: str       # one-sentence hint for TL's re-plan; NOT a full alternative


class ChallengerVerdict(TypedDict, total=False):
    """Structured output from the Challenger LLM.

    `verdict`:
      - "accept" — TL's plan looks fine; commander dispatches downstream
      - "block"  — at least one P0 objection; route back to TL with objections
      - "warn"   — concerns logged but commander dispatches anyway

    Hard rules:
      - verdict="block" REQUIRES ≥1 objection with severity="P0" and non-empty evidence
      - verdict="warn" REQUIRES ≥1 objection (any severity)
      - verdict="accept" → objections SHOULD be empty (warns get classified as warn)

    `dry_run_evidence` (optional):
      Result of running TL's verify_command in a fresh sandbox state,
      attached so future debugging shows what the Challenger saw.
    """

    verdict: ChallengerVerdictKind
    objections: list[ChallengerObjection]
    dry_run_evidence: dict | None
    notes: str            # one-line free-form summary; informational only


CHALLENGER_MAX_RETRY = 1
CHALLENGER_MAX_COST_USD = 5.0
CHALLENGER_MODEL_DEFAULT = "claude-sonnet-4-6"
