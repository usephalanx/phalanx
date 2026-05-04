"""v1.7.2.3 — Patch safety guards for the engineer step interpreter.

Engineer is constrained to execute TL's plan. But TL's plan is LLM-emitted
and can drift into shapes we never want to apply, even unintentionally:

  - editing CI config (.github/workflows/, .codecov.yml, etc.)
  - deleting test files outright
  - injecting `pytest.skip` / `@pytest.mark.skip` / `pytestmark = skip`
    into tests so the failing assertion stops running
  - editing files outside the `allowed_files` allowlist TL declared

These guards run BEFORE applying any step. A blocked step short-circuits
the engineer task; the agent reports `committed: false, skipped_reason:
patch_safety_violation:<rule>` so commander can escalate to a human.

The guards are deterministic (no LLM, no scope creep) and tier-1 testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────────────
# Path-based blocks
# ─────────────────────────────────────────────────────────────────────────────

# Files we never edit — CI config, coverage thresholds, lint config.
# A bug fix is for *application code*; if the failure is rooted in CI
# config drift, that's a human decision (escalate, don't patch silently).
_BLOCKED_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.github/workflows/.*\.ya?ml$"),
    re.compile(r"^\.github/dependabot\.ya?ml$"),
    re.compile(r"^\.codecov\.ya?ml$"),
    re.compile(r"^codecov\.ya?ml$"),
    re.compile(r"^\.pre-commit-config\.ya?ml$"),
)


def _is_test_path(path: str) -> bool:
    """True if path looks like a test file. Conservative — we'd rather
    over-block than under-block on test deletion.
    """
    if not path:
        return False
    if path.startswith("test_") or path.endswith("_test.py"):
        return True
    parts = path.split("/")
    if "tests" in parts or "test" in parts:
        return True
    if any(p.startswith("test_") for p in parts):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Content-based blocks (skip-injection)
# ─────────────────────────────────────────────────────────────────────────────
#
# These match suspicious *new* content the engineer would be writing.
# If TL's `new` (replace) or step.content (insert) or `diff` (apply_diff)
# contains a pytest skip directive, refuse the patch.

_SKIP_DIRECTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@pytest\.mark\.skip\b"),
    re.compile(r"@pytest\.mark\.skipif\b"),
    re.compile(r"@pytest\.mark\.xfail\b"),
    re.compile(r"@unittest\.skip\b"),
    re.compile(r"@unittest\.skipIf\b"),
    re.compile(r"@unittest\.expectedFailure\b"),
    re.compile(r"\bpytest\.skip\("),
    re.compile(r"\bpytest\.xfail\("),
    re.compile(r"^\s*pytestmark\s*=\s*pytest\.mark\.skip", re.MULTILINE),
)


# ─────────────────────────────────────────────────────────────────────────────
# v1.7.2.6 — test-content preservation patterns
# ─────────────────────────────────────────────────────────────────────────────
#
# R6 (assertion_reduction) and R7 (test_function_reduction) catch the
# subtler test-weakening shapes R3 (whole-file/lines deletion) misses:
# replace/apply_diff steps that drop assertions or remove `def test_`
# blocks while staying inside a test file.
#
# Both rules ONLY fire on test paths (R3 already blocks delete_lines on
# tests; here we cover replace + apply_diff). Both honor an opt-out at
# the step level: `step["allow_test_reduction"] = True`. TL must set
# this explicitly when a removal IS the right answer (e.g., consolidating
# duplicate assertions, removing a test that's now genuinely obsolete
# after a public-API rename).

# Counts both Python's `assert` keyword AND unittest-style methods
# (assertEqual, assertTrue, assertRaises, assertIsNone, ...). The
# regex matches `\bassert` followed by zero or more word chars +
# whitespace/paren — so it's deliberately permissive.
#
# False-positive guard: comments and docstrings get counted too. That's
# acceptable — the goal is "did the patch reduce assertion-shaped lines?"
# Numerically conservative (over-count → harder to "lose" assertions
# accidentally).
_ASSERTION_RE = re.compile(r"\bassert\w*\b")

# Matches a test function definition. Handles both module-level functions
# and class methods (with leading whitespace via MULTILINE):
#   def test_foo():
#       def test_method(self):
_TEST_FUNC_DEF_RE = re.compile(r"^\s*def\s+test_\w+\s*\(", re.MULTILINE)


def _count_assertions(text: str) -> int:
    return len(_ASSERTION_RE.findall(text or ""))


def _count_test_funcs(text: str) -> int:
    return len(_TEST_FUNC_DEF_RE.findall(text or ""))


def _diff_added_removed(diff_text: str) -> tuple[str, str]:
    """Split a unified diff into (added_lines_only, removed_lines_only).
    Skips the file-header lines `--- a/...` / `+++ b/...`.
    """
    if not diff_text:
        return "", ""
    added: list[str] = []
    removed: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return "\n".join(added), "\n".join(removed)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SafetyVerdict:
    ok: bool
    rule: str | None = None
    detail: str | None = None


_OK = SafetyVerdict(ok=True)


def validate_step_safety(
    step: dict, *, allowed_files: list[str] | None = None
) -> SafetyVerdict:
    """Returns SafetyVerdict.ok=False with `rule` + `detail` on rejection.

    Seven rules enforced:
      R1. blocked_path           — CI/coverage config files
      R2. allowlist_miss         — TL declared allowed_files but step.path isn't in it
      R3. test_deletion          — deleting a whole test file or its lines
      R4. skip_injection         — adding @pytest.skip / etc. to test content
      R5. workflow_in_diff       — apply_diff with a path under .github/workflows/
      R6. assertion_reduction    — replace/apply_diff drops assert count on a test file
      R7. test_function_reduction — replace/apply_diff removes `def test_<name>` blocks

    Run, commit, push steps have no path → always OK.

    Override: TL can set `step['allow_test_reduction'] = True` to bypass
    R6 + R7 when a removal IS the right answer (consolidation, public-API
    rename, escalation-approved cleanup). The override is per-step and must
    be set EXPLICITLY by TL in its task_plan; engineer cannot synthesize it.
    """
    action = step.get("action") or ""
    path = step.get("file") or step.get("path") or ""
    allow_test_reduction = step.get("allow_test_reduction") is True

    # R5 — apply_diff smuggling: action='apply_diff' has no file but the
    # diff text itself names paths (`+++ b/<path>`). Catch CI config there.
    if action == "apply_diff":
        diff = step.get("diff") or ""
        for pat in _BLOCKED_PATH_PATTERNS:
            # Look for the path-pattern fragment in the diff body.
            # e.g. "+++ b/.github/workflows/ci.yml"
            if re.search(rf"\+\+\+\s+b?/?{pat.pattern.lstrip('^').rstrip('$')}", diff):
                return SafetyVerdict(
                    ok=False,
                    rule="workflow_in_diff",
                    detail=f"apply_diff touches blocked path matching {pat.pattern!r}",
                )
        added_text, removed_text = _diff_added_removed(diff)
        # Skip-directive scan in the additions
        for pat in _SKIP_DIRECTIVE_PATTERNS:
            if pat.search(added_text):
                return SafetyVerdict(
                    ok=False,
                    rule="skip_injection",
                    detail=f"apply_diff adds skip directive matching {pat.pattern!r}",
                )
        # R6 + R7 on apply_diff body — only fire when the diff touches a
        # test file (detect via `+++ b/<path>` line in the diff).
        if not allow_test_reduction:
            diff_path_match = re.search(
                r"^\+\+\+\s+b?/?(\S+)", diff, re.MULTILINE
            )
            diff_path = diff_path_match.group(1) if diff_path_match else ""
            if diff_path and _is_test_path(diff_path):
                # R7 first (more specific) — see same-rule comment in
                # the `replace` path below.
                old_funcs = _count_test_funcs(removed_text)
                new_funcs = _count_test_funcs(added_text)
                if new_funcs < old_funcs:
                    return SafetyVerdict(
                        ok=False,
                        rule="test_function_reduction",
                        detail=(
                            f"apply_diff on test file {diff_path!r} removes "
                            f"{old_funcs} `def test_<name>` block(s) and adds "
                            f"only {new_funcs}. Set step.allow_test_reduction"
                            f"=true (escalation-only) if removal is intended."
                        ),
                    )
                old_asserts = _count_assertions(removed_text)
                new_asserts = _count_assertions(added_text)
                if new_asserts < old_asserts:
                    return SafetyVerdict(
                        ok=False,
                        rule="assertion_reduction",
                        detail=(
                            f"apply_diff on test file {diff_path!r} removes "
                            f"{old_asserts} assertion(s) and adds only "
                            f"{new_asserts}. Add equivalent assertions OR set "
                            f"step.allow_test_reduction=true (escalation-only)."
                        ),
                    )
        return _OK

    # No path-bearing step (run, commit, push) — nothing to check.
    if not path:
        return _OK

    # R1 — blocked paths (CI, coverage, hooks)
    for pat in _BLOCKED_PATH_PATTERNS:
        if pat.search(path):
            return SafetyVerdict(
                ok=False,
                rule="blocked_path",
                detail=f"path {path!r} matches blocked pattern {pat.pattern!r}",
            )

    # R2 — allowlist (only enforced if TL provided one)
    if allowed_files and path not in allowed_files:
        return SafetyVerdict(
            ok=False,
            rule="allowlist_miss",
            detail=(
                f"path {path!r} not in allowed_files {allowed_files!r}; "
                f"TL declared the allowlist; engineer cannot edit outside it"
            ),
        )

    # R3 — test deletion
    if _is_test_path(path) and action in {"delete_lines", "delete_file"}:
        return SafetyVerdict(
            ok=False,
            rule="test_deletion",
            detail=f"refusing to delete from test file {path!r} (action={action!r})",
        )

    # R4 — skip-directive injection in new content
    new_content = step.get("new") or step.get("content") or ""
    if isinstance(new_content, str):
        for pat in _SKIP_DIRECTIVE_PATTERNS:
            if pat.search(new_content):
                return SafetyVerdict(
                    ok=False,
                    rule="skip_injection",
                    detail=(
                        f"new content for {path!r} contains skip directive "
                        f"matching {pat.pattern!r}"
                    ),
                )

    # R7 — test_function reduction in `replace` step on a test file.
    #   Compares `def test_<name>(` blocks. Checked BEFORE R6 because
    #   removing a whole test function is the more-specific signal —
    #   R6 would also catch the assertion drop, but R7's detail message
    #   is more actionable.
    # R6 — assertion reduction in `replace` step on a test file.
    #   Compares assert-shaped lines in step.old vs step.new.
    #   Both rules pass when step.allow_test_reduction=True.
    if action == "replace" and _is_test_path(path) and not allow_test_reduction:
        old_text = step.get("old") or ""
        new_text = step.get("new") or ""
        if isinstance(old_text, str) and isinstance(new_text, str):
            old_funcs = _count_test_funcs(old_text)
            new_funcs = _count_test_funcs(new_text)
            if new_funcs < old_funcs:
                return SafetyVerdict(
                    ok=False,
                    rule="test_function_reduction",
                    detail=(
                        f"replace on test file {path!r} reduces `def test_` "
                        f"count ({old_funcs} → {new_funcs}). Set "
                        f"step.allow_test_reduction=true (escalation-only) "
                        f"if removal is intended."
                    ),
                )
            old_asserts = _count_assertions(old_text)
            new_asserts = _count_assertions(new_text)
            if new_asserts < old_asserts:
                return SafetyVerdict(
                    ok=False,
                    rule="assertion_reduction",
                    detail=(
                        f"replace on test file {path!r} reduces assert count "
                        f"({old_asserts} → {new_asserts}). Either keep an "
                        f"equivalent number of assertions OR set "
                        f"step.allow_test_reduction=true (escalation-only)."
                    ),
                )

    return _OK


__all__ = ["SafetyVerdict", "validate_step_safety"]
