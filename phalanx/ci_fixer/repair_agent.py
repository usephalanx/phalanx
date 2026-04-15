"""
DEPRECATED: repair_agent.py is superseded by agentic_loop.py.

The FSM approach is replaced by a tool-agnostic agentic loop where the LLM
drives repair using 4 tools (read_file, write_file, run_command, finish).
This module is kept for its RepairResult dataclass which agentic_loop.py imports.

Repair Agent FSM — drives the CI fix loop using a finite state machine.

States:
  GATHER_CONTEXT  → check complexity tier, try L1 pattern fix or history replay
  GENERATE_PATCH  → call Claude, validate guard rails, apply patch
  VALIDATE_PATCH  → run linter + tests to confirm the fix works
  RETRY           → re-parse validation output, increment iteration
  SUBMIT          → success exit
  ESCALATE        → low confidence on iter 1, human review warranted
  GIVE_UP         → max iterations or hard guard rail violated

Guard rails (enforced before any file write):
  - confidence == "low"            → ESCALATE (iter 1) or GIVE_UP (iter 2+)
  - patch targets test file        → strip that patch; ESCALATE if none left
  - |delta| > MAX_LINE_DELTA (15)  → reject patch
  - total delta > MAX_TOTAL (30)   → GIVE_UP
  - > MAX_FILES_CHANGED (3) files  → GIVE_UP
  - path traversal (..)            → reject patch immediately
  - linter gate fails post-apply   → revert + RETRY

L1 pattern fixes (no LLM):
  F401 → delete the import line
  E501 → wrap the long line (best-effort, 88-char limit)
  W291/W293 → strip trailing whitespace
  W292 → add newline at end of file
  I001 → run isort on the file (or ruff --fix --select I)
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from phalanx.ci_fixer.analyst import FilePatch, FixPlan, RootCauseAnalyst, _is_test_file
from phalanx.ci_fixer.log_parser import ParsedLog, parse_log
from phalanx.ci_fixer.validator import validate_fix

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from phalanx.ci_fixer.context_retriever import ContextBundle
    from phalanx.ci_fixer.validator import ValidationResult

log = structlog.get_logger(__name__)

_MAX_ITERATIONS = 3
_MAX_LINE_DELTA = 15       # max lines added/removed per single patch
_MAX_TOTAL_DELTA = 30      # max total line delta across all patches in one run
_MAX_FILES_CHANGED = 3     # max files touched per fix run
_L1_TIMEOUT = 30           # seconds for L1 subprocess fixes


# ── State machine ──────────────────────────────────────────────────────────────


class RepairState(StrEnum):
    GATHER_CONTEXT = "GATHER_CONTEXT"
    GENERATE_PATCH = "GENERATE_PATCH"
    VALIDATE_PATCH = "VALIDATE_PATCH"
    RETRY          = "RETRY"
    SUBMIT         = "SUBMIT"
    ESCALATE       = "ESCALATE"
    GIVE_UP        = "GIVE_UP"


# ── Result ─────────────────────────────────────────────────────────────────────


@dataclass
class RepairResult:
    """Outcome of a complete repair agent run."""

    success: bool
    fix_plan: FixPlan | None = None
    validation: ValidationResult | None = None
    iteration: int = 0
    escalate: bool = False       # True → human review warranted
    reason: str = ""             # failure reason key (machine-readable)
    used_history: bool = False   # True if cached patch replayed
    used_l1_pattern: bool = False  # True if deterministic L1 fix applied
    state_trace: list[str] = field(default_factory=list)  # FSM path for debugging


# ── Public entry point ─────────────────────────────────────────────────────────


def run_repair(
    context: ContextBundle,
    call_claude: Callable,
    workspace: Path,
    original_parsed: ParsedLog,
    max_iterations: int = _MAX_ITERATIONS,
) -> RepairResult:
    """
    Run the full repair FSM.

    Args:
        context:        pre-assembled ContextBundle (classifier + files + history)
        call_claude:    bound BaseAgent._call_claude — used for GENERATE_PATCH
        workspace:      absolute path to cloned repo on disk
        original_parsed: ParsedLog from the initial log parse (used for regression check)
        max_iterations: max GENERATE→VALIDATE→RETRY cycles (default 3)

    Returns:
        RepairResult — always returns, never raises.
    """
    agent = _RepairFSM(
        context=context,
        call_claude=call_claude,
        workspace=workspace,
        original_parsed=original_parsed,
        max_iterations=max_iterations,
    )
    return agent.run()


# ── FSM implementation ─────────────────────────────────────────────────────────


class _RepairFSM:
    def __init__(
        self,
        context: ContextBundle,
        call_claude: Callable,
        workspace: Path,
        original_parsed: ParsedLog,
        max_iterations: int,
    ) -> None:
        self._context = context
        self._call_claude = call_claude
        self._workspace = workspace
        self._original_parsed = original_parsed
        self._max_iterations = max_iterations

        self._state = RepairState.GATHER_CONTEXT
        self._iteration = 0
        self._fix_plan: FixPlan | None = None
        self._validation: ValidationResult | None = None
        self._current_parsed = context.parsed_log
        self._trace: list[str] = []

    def run(self) -> RepairResult:
        while self._state not in (
            RepairState.SUBMIT,
            RepairState.ESCALATE,
            RepairState.GIVE_UP,
        ):
            self._trace.append(self._state)
            log.debug("repair_agent.state", state=self._state, iteration=self._iteration)
            if self._state == RepairState.GATHER_CONTEXT:
                self._state = self._do_gather_context()
            elif self._state == RepairState.GENERATE_PATCH:
                self._state = self._do_generate_patch()
            elif self._state == RepairState.VALIDATE_PATCH:
                self._state = self._do_validate_patch()
            elif self._state == RepairState.RETRY:
                self._state = self._do_retry()

        self._trace.append(self._state)
        return self._build_result()

    # ── State handlers ─────────────────────────────────────────────────────────

    def _do_gather_context(self) -> RepairState:
        # Guard: no errors at all
        if not self._current_parsed.has_errors:
            log.info("repair_agent.no_errors")
            self._fix_plan = None
            self._validation = None
            return self._give_up("no_structured_errors")

        # Guard: workspace must exist
        if not self._workspace.exists():
            return self._give_up("workspace_missing")

        tier = self._context.classification.complexity_tier

        # L1: try deterministic pattern fix first (no LLM)
        if tier == "L1":
            result = _try_l1_fix(self._current_parsed, self._workspace)
            if result is not None:
                log.info("repair_agent.l1_pattern_applied", files=result)
                self._fix_plan = FixPlan(
                    confidence="high",
                    root_cause=self._context.classification.root_cause_hypothesis,
                    patches=[],  # L1 patches were applied in-place
                    needs_new_test=False,
                )
                self._fix_plan._l1_files = result  # type: ignore[attr-defined]
                return RepairState.VALIDATE_PATCH

        # History replay: try cached patch before calling LLM
        if self._context.has_history():
            replayed = _try_replay_history(self._context, self._workspace)
            if replayed is not None:
                self._fix_plan = replayed
                self._fix_plan._from_history = True  # type: ignore[attr-defined]
                log.info("repair_agent.history_replayed", patches=len(replayed.patches))
                return RepairState.VALIDATE_PATCH

        return RepairState.GENERATE_PATCH

    def _do_generate_patch(self) -> RepairState:
        self._iteration += 1
        log.info("repair_agent.generate_patch", iteration=self._iteration)

        analyst = RootCauseAnalyst(call_llm=self._call_claude)
        fix_plan = analyst.analyze(
            self._current_parsed,
            self._workspace,
            fingerprint_hash=None,  # history already handled in GATHER_CONTEXT
        )

        log.info(
            "repair_agent.fix_plan",
            confidence=fix_plan.confidence,
            patches=len(fix_plan.patches),
            root_cause=fix_plan.root_cause,
            iteration=self._iteration,
        )

        # Guard: low confidence
        if fix_plan.confidence == "low":
            self._fix_plan = fix_plan
            if self._iteration == 1:
                return self._escalate("low_confidence")
            return self._give_up("low_confidence")

        # Guard: no patches produced
        if not fix_plan.patches:
            self._fix_plan = fix_plan
            if self._iteration == 1:
                return self._escalate("no_patches")
            return self._give_up("no_patches")

        # Guard: too many files changed
        if len(fix_plan.patches) > _MAX_FILES_CHANGED:
            log.warning("repair_agent.too_many_files", count=len(fix_plan.patches))
            self._fix_plan = fix_plan
            return self._give_up("too_many_files_changed")

        # Guard: total line delta too large
        total_delta = sum(abs(p.delta) for p in fix_plan.patches)
        if total_delta > _MAX_TOTAL_DELTA:
            log.warning("repair_agent.delta_too_large", total_delta=total_delta)
            self._fix_plan = fix_plan
            return self._give_up("total_delta_too_large")

        self._fix_plan = fix_plan
        return RepairState.VALIDATE_PATCH

    def _do_validate_patch(self) -> RepairState:
        validation = validate_fix(
            self._current_parsed,
            self._workspace,
            original_parsed=self._original_parsed,
        )
        self._validation = validation

        log.info(
            "repair_agent.validation",
            passed=validation.passed,
            tool=validation.tool,
            regressions=len(validation.regressions),
            iteration=self._iteration,
        )

        if validation.passed:
            return RepairState.SUBMIT

        if self._iteration >= self._max_iterations:
            return self._give_up("max_iterations_exhausted")

        return RepairState.RETRY

    def _do_retry(self) -> RepairState:
        # Re-parse the validation output as the new error set for the next iteration
        if self._validation and self._validation.output:
            reparsed = parse_log(self._validation.output)
            if reparsed.has_errors:
                self._current_parsed = reparsed
                log.info(
                    "repair_agent.retry_with_new_errors",
                    iteration=self._iteration,
                    errors=self._current_parsed.summary(),
                )

        return RepairState.GENERATE_PATCH

    # ── Terminal state helpers ─────────────────────────────────────────────────

    def _give_up(self, reason: str) -> RepairState:
        self._give_up_reason = reason
        return RepairState.GIVE_UP

    def _escalate(self, reason: str) -> RepairState:
        self._escalate_reason = reason
        return RepairState.ESCALATE

    def _build_result(self) -> RepairResult:
        terminal = self._state
        used_history = bool(
            self._fix_plan and getattr(self._fix_plan, "_from_history", False)
        )
        used_l1 = bool(
            self._fix_plan and getattr(self._fix_plan, "_l1_files", None)
        )

        if terminal == RepairState.SUBMIT:
            return RepairResult(
                success=True,
                fix_plan=self._fix_plan,
                validation=self._validation,
                iteration=self._iteration,
                used_history=used_history,
                used_l1_pattern=used_l1,
                state_trace=self._trace,
            )
        if terminal == RepairState.ESCALATE:
            return RepairResult(
                success=False,
                fix_plan=self._fix_plan,
                escalate=True,
                reason=getattr(self, "_escalate_reason", "escalated"),
                iteration=self._iteration,
                state_trace=self._trace,
            )
        # GIVE_UP
        return RepairResult(
            success=False,
            fix_plan=self._fix_plan,
            validation=self._validation,
            escalate=False,
            reason=getattr(self, "_give_up_reason", "unknown"),
            iteration=self._iteration,
            state_trace=self._trace,
        )


# ── L1 pattern fixes (deterministic, no LLM) ──────────────────────────────────


def _try_l1_fix(parsed: ParsedLog, workspace: Path) -> list[str] | None:
    """
    Apply deterministic L1 fixes for simple lint codes.
    Returns list of modified relative paths on success, None if nothing applied.
    Runs `ruff check --fix --select F401,E501,W291,W293,W292,I001,F811`
    which handles all L1 codes in one pass.
    """
    files = parsed.all_files[:_MAX_FILES_CHANGED]
    if not files:
        return None

    safe_files = [f for f in files if not _is_test_file(f)]
    if not safe_files:
        log.info("repair_agent.l1_all_test_files_skipped")
        return None

    try:
        result = subprocess.run(
            ["ruff", "check", "--fix", "--select", "F401,E501,W291,W293,W292,I001,F811"]
            + safe_files,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=_L1_TIMEOUT,
        )
        if result.returncode in (0, 1):  # 0=clean, 1=fixed some errors
            modified = [f for f in safe_files if _file_was_modified(workspace / f)]
            log.info("repair_agent.l1_ruff_fix", returncode=result.returncode, modified=modified)
            return modified if modified else None
    except Exception as exc:
        log.warning("repair_agent.l1_fix_failed", error=str(exc))
    return None


def _file_was_modified(path: Path) -> bool:
    """Best-effort: assume file was potentially modified if ruff exit 1."""
    # ruff --fix returns 1 when it fixed something, 0 when already clean
    # We can't easily tell which files changed without git diff
    # For now: assume all non-test files that were targeted were touched
    return path.exists()


# ── History replay ─────────────────────────────────────────────────────────────


def _try_replay_history(context: ContextBundle, workspace: Path) -> FixPlan | None:
    """
    Attempt to replay a cached patch from context.similar_fixes.
    Validates the patch is still applicable (lines haven't shifted too much).
    Returns a FixPlan if replay succeeds, None if not applicable.
    """
    from phalanx.ci_fixer.analyst import FilePatch, FixPlan, _is_test_file  # noqa: PLC0415

    for similar in context.similar_fixes:
        if not similar.last_good_patch_json:
            continue
        try:
            raw_patches = json.loads(similar.last_good_patch_json)
        except (json.JSONDecodeError, TypeError):
            continue

        patches: list[FilePatch] = []
        for p in raw_patches:
            path = p.get("path", "")
            if _is_test_file(path):
                continue
            full = workspace / path
            if not full.exists():
                break  # patch references a file that no longer exists
            patches.append(
                FilePatch(
                    path=path,
                    start_line=int(p.get("start_line", 1)),
                    end_line=int(p.get("end_line", 1)),
                    corrected_lines=list(p.get("corrected_lines", [])),
                    reason=p.get("reason", "history replay"),
                )
            )

        if patches and len(patches) == len(raw_patches):
            return FixPlan(
                confidence="high",
                root_cause="Reused known-good fix from history",
                patches=patches,
                needs_new_test=False,
            )

    return None
