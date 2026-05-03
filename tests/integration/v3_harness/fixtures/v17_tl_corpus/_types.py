"""Shared types for the v1.7 TL output corpus."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class CorpusFixture:
    """One CI failure shape with the inputs TL sees + invariants its output
    must satisfy.

    `ci_log_text` is what TL gets back from fetch_ci_log. `repo_files` is
    the file-system state TL can read_file against (path → content). The
    harness wires these into a synthetic AgentContext.

    `invariants` is a list of assertion callables. Each takes the TL
    output dict and either returns silently (passing) or raises
    AssertionError with a clear message. Callables receive the output
    AFTER plan_validator has already passed — focus on semantic checks.
    """

    name: str
    description: str
    source_repo: str
    source_pr_or_commit: str           # for human reference + reproducibility
    complexity: str                    # "simple" | "medium" | "complex"

    # TL inputs
    ci_log_text: str
    repo_files: dict[str, str]         # path → content (TL reads via read_file)
    failing_command: str
    failing_job_name: str
    pr_number: int                     # synthetic; the harness fills the rest

    # Invariants
    must_pass_plan_validator: bool = True
    expected_review_decision_in_replan: bool = False  # set True for fixtures where ESCALATE is the right answer
    invariants: list[Callable[[dict], None]] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Common invariant builders — small, composable assertions.
# Each returns a callable suitable for CorpusFixture.invariants.
# ─────────────────────────────────────────────────────────────────────


def root_cause_mentions(*keywords: str):
    """root_cause text must include each keyword (case-insensitive)."""

    def _check(output: dict) -> None:
        rc = (output.get("root_cause") or "").lower()
        for kw in keywords:
            assert kw.lower() in rc, (
                f"root_cause missing keyword {kw!r}; got: {rc!r}"
            )

    _check.__name__ = f"root_cause_mentions({', '.join(keywords)})"
    return _check


def plan_includes_agent(agent: str, *, min_count: int = 1):
    """task_plan has at least `min_count` task(s) with the given agent role."""

    def _check(output: dict) -> None:
        plan = output.get("task_plan") or []
        n = sum(1 for t in plan if t.get("agent") == agent)
        assert n >= min_count, (
            f"task_plan must include ≥ {min_count} {agent!r} task(s); got {n}"
        )

    _check.__name__ = f"plan_includes_agent({agent}, min={min_count})"
    return _check


def plan_excludes_agent(agent: str):
    """task_plan must NOT include any task with the given agent role.

    Useful for the 'no SRE setup needed' shapes.
    """

    def _check(output: dict) -> None:
        plan = output.get("task_plan") or []
        n = sum(1 for t in plan if t.get("agent") == agent)
        assert n == 0, (
            f"task_plan must NOT include {agent!r}; got {n} such task(s)"
        )

    _check.__name__ = f"plan_excludes_agent({agent})"
    return _check


def plan_steps_modify(file_path: str):
    """At least one engineer task has steps that modify the given file
    (replace/insert/delete_lines/apply_diff with file=path).

    apply_diff is treated as an unknown-file-target; we accept it if the
    diff text mentions the path.
    """

    def _check(output: dict) -> None:
        plan = output.get("task_plan") or []
        for ts in plan:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                action = step.get("action")
                if action in {"replace", "insert", "delete_lines"} and step.get("file") == file_path:
                    return
                if action == "apply_diff" and file_path in (step.get("diff") or ""):
                    return
        raise AssertionError(
            f"no engineer step modifies {file_path!r} in task_plan"
        )

    _check.__name__ = f"plan_steps_modify({file_path})"
    return _check


def plan_does_not_modify_path_prefix(prefix: str):
    """No engineer step touches a file under the given prefix.

    Used for `.github/workflows/` — TL must escalate, not edit CI config.
    """

    def _check(output: dict) -> None:
        plan = output.get("task_plan") or []
        for ts in plan:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                f = step.get("file") or ""
                if f.startswith(prefix):
                    raise AssertionError(
                        f"engineer step modifies forbidden path {f!r} (prefix {prefix!r})"
                    )
                if step.get("action") == "apply_diff":
                    diff = step.get("diff") or ""
                    if prefix in diff:
                        raise AssertionError(
                            f"engineer apply_diff references forbidden prefix {prefix!r}"
                        )

    _check.__name__ = f"plan_does_not_modify_path_prefix({prefix})"
    return _check


def env_requirements_includes_python_package(pkg: str):
    """The env_requirements (top-level OR in any sre_setup task) must
    include the given pip package name. Catches "TL identified a missing
    dep but forgot to ask SRE to install it" bugs.
    """

    def _check(output: dict) -> None:
        envs: list[dict] = []
        top = output.get("env_requirements")
        if isinstance(top, dict):
            envs.append(top)
        for ts in output.get("task_plan") or []:
            if ts.get("agent") == "cifix_sre_setup":
                env = ts.get("env_requirements")
                if isinstance(env, dict):
                    envs.append(env)
        for env in envs:
            for p in env.get("python_packages") or []:
                if pkg.lower() in p.lower():
                    return
        raise AssertionError(
            f"env_requirements.python_packages must include {pkg!r}; "
            f"got envs={envs}"
        )

    _check.__name__ = f"env_requirements_includes_python_package({pkg})"
    return _check


def confidence_at_least(threshold: float):
    """confidence ≥ threshold."""

    def _check(output: dict) -> None:
        c = float(output.get("confidence") or 0.0)
        assert c >= threshold, f"confidence {c} below threshold {threshold}"

    _check.__name__ = f"confidence_at_least({threshold})"
    return _check


def confidence_at_most(threshold: float):
    """confidence ≤ threshold (used for shapes that should ESCALATE)."""

    def _check(output: dict) -> None:
        c = float(output.get("confidence") or 0.0)
        assert c <= threshold, (
            f"confidence {c} above threshold {threshold} (this fixture "
            f"should escalate, not commit a fix)"
        )

    _check.__name__ = f"confidence_at_most({threshold})"
    return _check


def step_count_in_engineer_task_at_least(n: int):
    """The (first) engineer task in the plan has ≥ n steps. Catches TL
    that emits 'replace' alone without commit/push.
    """

    def _check(output: dict) -> None:
        for ts in output.get("task_plan") or []:
            if ts.get("agent") == "cifix_engineer":
                steps = ts.get("steps") or []
                assert len(steps) >= n, (
                    f"engineer task has {len(steps)} steps; need ≥ {n}"
                )
                return
        raise AssertionError("no cifix_engineer task in plan to check")

    _check.__name__ = f"step_count_in_engineer_task_at_least({n})"
    return _check


def engineer_task_includes_action(action: str):
    """The engineer task must include at least one step with the given action.
    Useful for 'must include commit', 'must include push' invariants.
    """

    def _check(output: dict) -> None:
        for ts in output.get("task_plan") or []:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                if step.get("action") == action:
                    return
        raise AssertionError(
            f"no engineer step has action={action!r} in any engineer task"
        )

    _check.__name__ = f"engineer_task_includes_action({action})"
    return _check


def affected_files_is_empty():
    """affected_files MUST be []. Used for env-mismatch fixtures where TL
    correctly identifies the failure as environmental, not a code bug.
    """

    def _check(output: dict) -> None:
        af = output.get("affected_files")
        assert af == [], (
            f"affected_files must be [] for env-mismatch fixtures; got {af!r}"
        )

    _check.__name__ = "affected_files_is_empty"
    return _check


def open_questions_mentions(*keywords: str):
    """Each keyword (case-insensitive) appears in at least one open_questions
    entry. Used to verify TL surfaced the right concern (e.g., "sandbox lacks uv").
    """

    def _check(output: dict) -> None:
        oq = output.get("open_questions") or []
        joined = " ".join(str(q) for q in oq).lower()
        for kw in keywords:
            assert kw.lower() in joined, (
                f"open_questions must mention {kw!r}; got: {oq}"
            )

    _check.__name__ = f"open_questions_mentions({', '.join(keywords)})"
    return _check


def review_decision_equals(expected: str):
    """review_decision MUST equal expected. Used for fixtures where TL
    is supposed to ESCALATE from PLAN mode (env-mismatch shape).
    """

    def _check(output: dict) -> None:
        got = output.get("review_decision")
        assert got == expected, (
            f"review_decision must be {expected!r}; got {got!r}"
        )

    _check.__name__ = f"review_decision_equals({expected})"
    return _check


def verify_command_targets_broader_than_failing():
    """For 'delete test' fixes: verify_command must NOT equal failing_command.
    The failing_command targets the deleted test (would exit 4); verify must
    broaden to a parent path or whole-suite command.
    """

    def _check(output: dict) -> None:
        fc = output.get("failing_command") or ""
        vc = output.get("verify_command") or ""
        assert fc and vc, (
            f"both failing_command and verify_command must be present; "
            f"got failing={fc!r}, verify={vc!r}"
        )
        assert vc != fc, (
            f"delete-test fix requires broader verify_command; "
            f"failing_command and verify_command are identical: {fc!r}"
        )

    _check.__name__ = "verify_command_targets_broader_than_failing"
    return _check


def plan_creates_file(file_path: str):
    """At least one engineer step writes a NEW file at the given path.
    Insert/apply_diff with a path not in the original repo counts.
    """

    def _check(output: dict) -> None:
        plan = output.get("task_plan") or []
        for ts in plan:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                action = step.get("action")
                # 'insert' on a new file or apply_diff that creates it both work.
                # For apply_diff, we look for a "new file mode" or "+++ b/<path>"
                # without a matching "--- a/<path>" — but that's hard to assert
                # without parsing diffs. Simpler: file == path and action is one
                # of the file-writing actions.
                if step.get("file") == file_path and action in {
                    "insert",
                    "replace",
                }:
                    return
                if action == "apply_diff" and file_path in (step.get("diff") or ""):
                    return
        raise AssertionError(
            f"no engineer step creates/writes {file_path!r} in task_plan"
        )

    _check.__name__ = f"plan_creates_file({file_path})"
    return _check
