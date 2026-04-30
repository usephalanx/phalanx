"""Evidence checking for SRE install tools.

When the LLM calls install_apt / install_pip / install_via_curl, it MUST
supply (evidence_file, evidence_line) pointing to where in the repo the
tool/package is mentioned. This helper enforces that constraint at the
TOOL level, not the prompt level — see design doc §4 (gap #2).

Why it matters: LLMs reliably violate "only install what's evidenced"
prompt instructions. By making evidence verification a tool-side check
that returns an error result, we make the constraint enforceable.

What counts as "evidence":
  - The file (relative to repo root) actually exists
  - The line number is within the file's bounds
  - At least one of the candidate package/tool names appears in a small
    window around that line (default ±5 lines), case-insensitive,
    word-boundary aware

The window approach is intentionally lenient — workflow YAML often
splits a `uses: astral-sh/setup-uv@v8` and its companion `run: uvx tox`
across multiple lines. A 1-line exact match would be too strict for real
LLM tool usage. The package-name match anchors it to actual repo content.
"""

from __future__ import annotations

import re
from pathlib import Path

# How many lines of context above + below the cited line to scan.
# 5 is empirically chosen: covers the "uses + with + run" YAML pattern
# without drifting far enough to false-match unrelated content.
_DEFAULT_WINDOW_LINES = 5


def evidence_check(
    workspace_path: str | Path,
    evidence_file: str,
    evidence_line: int,
    candidates: list[str],
    *,
    window_lines: int = _DEFAULT_WINDOW_LINES,
) -> tuple[bool, str]:
    """Verify that at least one of `candidates` appears near (file:line).

    Args:
        workspace_path: HOST path to the cloned repo root.
        evidence_file: Repo-relative path (e.g., '.github/workflows/lint.yml').
            Path traversal (.., absolute paths) is rejected.
        evidence_line: 1-indexed line number from the LLM's tool call.
        candidates: List of strings to look for (e.g., ['uv', 'astral-sh/setup-uv']).
            Case-insensitive, word-boundary matched. Empty list always fails.
        window_lines: How many lines above + below `evidence_line` to scan.

    Returns:
        (ok, reason). reason is empty on success, descriptive on failure.
    """
    if not candidates:
        return False, "evidence_check: candidates list is empty"

    # Reject path traversal + absolute paths up front.
    if not evidence_file or evidence_file.startswith("/") or ".." in evidence_file.split("/"):
        return False, f"evidence_check: invalid evidence_file path: {evidence_file!r}"

    if evidence_line < 1:
        return False, f"evidence_check: line must be >=1, got {evidence_line}"

    workspace = Path(workspace_path).resolve()
    target = (workspace / evidence_file).resolve()

    # Belt-and-suspenders: even after path traversal rejection above, ensure
    # the resolved target actually lives under workspace_path.
    try:
        target.relative_to(workspace)
    except ValueError:
        return False, f"evidence_check: file resolves outside workspace: {evidence_file!r}"

    if not target.is_file():
        return False, f"evidence_check: file does not exist: {evidence_file}"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"evidence_check: failed to read {evidence_file}: {exc}"

    lines = text.splitlines()
    if evidence_line > len(lines):
        return False, (
            f"evidence_check: line {evidence_line} out of bounds for "
            f"{evidence_file} (only {len(lines)} lines)"
        )

    # Build a window around the cited line. evidence_line is 1-indexed.
    lo = max(0, evidence_line - 1 - window_lines)
    hi = min(len(lines), evidence_line + window_lines)
    window = "\n".join(lines[lo:hi]).lower()

    # Word-boundary match per candidate. Use \b to avoid matching `uv` inside
    # `uvloop`, but allow common separators (`-`, `_`, `/`) as boundaries
    # since action names like `astral-sh/setup-uv` aren't \w-boundaries.
    for cand in candidates:
        if not cand:
            continue
        # Escape regex chars in cand. Match either as a \b-bounded word OR
        # surrounded by characters from a permissive boundary set.
        # E.g., `uv` matches in `uses: setup-uv@v8` (preceded by `-`) but NOT
        # in `uvloop` (preceded by start-of-word, followed by alpha).
        pat = r"(?:^|[\s\-_/=:'\"@`(])" + re.escape(cand.lower()) + r"(?:$|[\s\-_/=:'\"@`)\.,;])"
        if re.search(pat, window):
            return True, ""

    return False, (
        f"evidence_check: none of {candidates!r} found within ±{window_lines} "
        f"lines of {evidence_file}:{evidence_line}"
    )
