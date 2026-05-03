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

    Five rules enforced:
      R1. blocked_path     — CI/coverage config files
      R2. allowlist_miss   — TL declared allowed_files but step.path isn't in it
      R3. test_deletion    — deleting a whole test file or its lines
      R4. skip_injection   — adding @pytest.skip / etc. to test content
      R5. workflow_in_diff — apply_diff with a path under .github/workflows/

    Run, commit, push steps have no path → always OK.
    """
    action = step.get("action") or ""
    path = step.get("file") or step.get("path") or ""

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
        # Skip-directive scan in the additions (lines starting with +)
        added = "\n".join(
            ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        )
        for pat in _SKIP_DIRECTIVE_PATTERNS:
            if pat.search(added):
                return SafetyVerdict(
                    ok=False,
                    rule="skip_injection",
                    detail=f"apply_diff adds skip directive matching {pat.pattern!r}",
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

    return _OK


__all__ = ["SafetyVerdict", "validate_step_safety"]
