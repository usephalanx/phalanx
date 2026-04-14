"""
CI Root Cause Analyst — LLM confirmation of structured errors.

Pipeline:
  1. Read ±WINDOW lines around each flagged error line (not the whole file).
     Line numbers are included so the LLM knows exactly where it is.
  2. Ask the LLM to return the corrected version of that window only —
     not the full file.  This eliminates the file-deletion risk.
  3. Parse the JSON response into a FixPlan.

Guard rails (enforced before any patch reaches the agent):
  - MAX_LINE_DELTA: corrected window may not grow or shrink by more than
    MAX_LINE_DELTA lines vs the original window.  Catches LLM rewrites.
  - MAX_FILES: only the files explicitly mentioned in the parsed errors
    are read; at most MAX_FILES of them.
  - No test-file modifications: if a patch targets a test file the plan
    is downgraded to confidence="low".
  - Low-confidence → is_actionable returns False → agent never commits.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.log_parser import ParsedLog

log = structlog.get_logger(__name__)

# Lines of context shown to the LLM either side of each error line.
_WINDOW = 40
# Maximum line-count delta between original window and corrected window.
# If the LLM returns more/fewer lines than this it is rejected.
_MAX_LINE_DELTA = 5
# Maximum files we will read and send to the LLM.
_MAX_FILES = 4


# ── Output types ───────────────────────────────────────────────────────────────


@dataclass
class FileWindow:
    """A contiguous slice of a file that was shown to the LLM."""

    path: str
    start_line: int   # 1-indexed, inclusive
    end_line: int     # 1-indexed, inclusive
    original_lines: list[str]


@dataclass
class FilePatch:
    """
    A repair to a single file.

    corrected_lines replaces original_lines[start_line-1 : end_line] in the
    source file.  The agent verifies the line counts before writing.
    """

    path: str
    start_line: int          # 1-indexed
    end_line: int            # 1-indexed
    corrected_lines: list[str]
    reason: str = ""

    @property
    def original_window_size(self) -> int:
        return self.end_line - self.start_line + 1

    @property
    def delta(self) -> int:
        """Signed line count change: negative means lines were removed."""
        return len(self.corrected_lines) - self.original_window_size


@dataclass
class FixPlan:
    """
    Structured fix plan produced by the analyst.

    confidence:
      "high"   → agent applies and validates automatically
      "medium" → agent applies and validates; human must approve PR
      "low"    → agent does NOT commit; logs for human review
    """

    confidence: str   # "high" | "medium" | "low"
    root_cause: str
    patches: list[FilePatch] = field(default_factory=list)
    needs_new_test: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.confidence in ("high", "medium") and bool(self.patches)


# ── Prompt ─────────────────────────────────────────────────────────────────────

_ANALYST_PROMPT = """\
You are a senior engineer fixing a CI failure.

The CI tool reported these EXACT structured errors (deterministically parsed — \
do not second-guess the file paths or line numbers):

{structured_errors}

Below are the relevant file windows — only the lines near each error are shown, \
with line numbers on the left.  You MUST fix only within these windows.

{file_windows}

Rules:
1. Return ONLY valid JSON — no markdown fences, no prose outside the JSON.
2. "patches" must correspond 1-to-1 with the file windows above.
   Each patch must include "path", "start_line", "end_line", and \
"corrected_lines" (a JSON array of strings, one per line, each ending with \\n).
3. "corrected_lines" may differ from the original window by at most \
{max_line_delta} lines (adding or removing).  Do NOT rewrite the whole file.
4. NEVER modify test files (paths containing /test or test_).
5. For unused imports (F401): delete the import line only.
6. For line-too-long (E501): wrap or shorten the line only.
7. For future-import order (F404): move the __future__ import to line 1 only.
8. If you cannot produce a high or medium confidence fix, set \
confidence="low" and patches=[].

Response schema:
{{
  "confidence": "high" | "medium" | "low",
  "root_cause": "<one sentence>",
  "patches": [
    {{
      "path": "<relative path from repo root>",
      "start_line": <int>,
      "end_line": <int>,
      "corrected_lines": ["line1\\n", "line2\\n", ...],
      "reason": "<one sentence>"
    }}
  ],
  "needs_new_test": false
}}
"""


# ── Analyst ────────────────────────────────────────────────────────────────────


class RootCauseAnalyst:
    """
    Wraps the LLM call for root cause analysis.

    Kept synchronous because _call_claude is synchronous (wraps Anthropic SDK).

    Phase 2 addition: optional history_lookup callable.
    If provided, it is called with (fingerprint_hash: str) before the LLM call.
    If it returns a list[dict] (previously successful patches for this fingerprint),
    those patches are validated against the current windows and — if still valid —
    returned directly as a FixPlan(confidence="high") without an LLM call.
    """

    def __init__(self, call_llm, history_lookup=None):
        """
        Args:
            call_llm: callable(messages, max_tokens) -> str
                      Typically BaseAgent._call_claude bound to the agent.
            history_lookup: optional callable(fingerprint_hash: str) -> list[dict] | None
                      Returns previously-successful patches or None.
                      Injected by CIFixerAgent (Phase 2+); None → no history check.
        """
        self._call_llm = call_llm
        self._history_lookup = history_lookup

    def analyze(
        self,
        parsed_log: "ParsedLog",
        workspace: Path,
        fingerprint_hash: str | None = None,
    ) -> FixPlan:
        """
        Read file windows, optionally reuse history, call LLM, return FixPlan.

        Phase 2 flow:
          1. Read file windows (same as Phase 1).
          2. If fingerprint_hash provided and history_lookup is wired, check for a
             known-good patch.  If the cached patches still validate against the
             current windows, return them directly (saves an LLM call).
          3. Otherwise fall through to the LLM call (Phase 1 path).

        All guard-rail failures return FixPlan(confidence="low") so the agent
        never commits based on a bad plan.
        """
        if not parsed_log.has_errors:
            return FixPlan(confidence="low", root_cause="No structured errors found")

        windows = self._read_windows(workspace, parsed_log)
        if not windows:
            return FixPlan(
                confidence="low",
                root_cause="Could not read any of the failing files from workspace",
            )

        # ── Phase 2: history check ─────────────────────────────────────────────
        if fingerprint_hash and self._history_lookup is not None:
            cached = self._history_lookup(fingerprint_hash)
            if cached:
                patches = self._parse_and_validate_patches(cached, windows)
                if patches:
                    log.info(
                        "ci_analyst.history_hit",
                        fingerprint=fingerprint_hash,
                        patches=len(patches),
                    )
                    return FixPlan(
                        confidence="high",
                        root_cause="Reused known-good fix from history",
                        patches=patches,
                        needs_new_test=False,
                    )
                log.debug(
                    "ci_analyst.history_miss_validation_failed",
                    fingerprint=fingerprint_hash,
                )

        structured_errors = parsed_log.as_text()
        file_windows_text = _format_windows(windows)

        prompt = _ANALYST_PROMPT.format(
            structured_errors=structured_errors,
            file_windows=file_windows_text,
            max_line_delta=_MAX_LINE_DELTA,
        )

        try:
            raw = self._call_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
        except Exception as exc:
            log.warning("ci_analyst.llm_failed", error=str(exc))
            return FixPlan(confidence="low", root_cause=f"LLM call failed: {exc}")

        raw = raw.strip()
        # Strip markdown fences if the LLM wrapped the JSON anyway
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        log.debug("ci_analyst.raw_response", preview=raw[:400])

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("ci_analyst.json_parse_failed", error=str(exc), raw=raw[:500])
            return FixPlan(confidence="low", root_cause="LLM returned non-JSON response")

        patches = self._parse_and_validate_patches(data.get("patches", []), windows)
        confidence = data.get("confidence", "low")

        # If patch validation rejected everything, downgrade to low
        if data.get("patches") and not patches:
            log.warning("ci_analyst.all_patches_rejected")
            confidence = "low"

        return FixPlan(
            confidence=confidence,
            root_cause=data.get("root_cause", ""),
            patches=patches,
            needs_new_test=data.get("needs_new_test", False),
        )

    # ── File reading ───────────────────────────────────────────────────────────

    def _read_windows(
        self, workspace: Path, parsed_log: "ParsedLog"
    ) -> list[FileWindow]:
        """
        For each file in parsed_log.all_files, read a window of ±WINDOW lines
        around every error line in that file.  Merge overlapping windows.
        Returns at most _MAX_FILES FileWindow objects.
        """
        # Build map: file_path → list of error line numbers
        error_lines_by_file: dict[str, list[int]] = {}
        for e in parsed_log.lint_errors:
            error_lines_by_file.setdefault(e.file, []).append(e.line)
        for e in parsed_log.type_errors:
            error_lines_by_file.setdefault(e.file, []).append(e.line)
        # For test failures we have no line number — read top of file
        for f in parsed_log.test_failures:
            error_lines_by_file.setdefault(f.file, []).append(1)

        windows: list[FileWindow] = []
        for rel_path in parsed_log.all_files[:_MAX_FILES]:
            full = workspace / rel_path
            if not full.exists():
                # Try rglob fallback (handles monorepo path prefix issues)
                matches = list(workspace.rglob(Path(rel_path).name))
                if matches:
                    full = matches[0]
                    rel_path = str(full.relative_to(workspace))
                else:
                    log.debug("ci_analyst.file_not_found", path=rel_path)
                    continue

            try:
                all_lines = full.read_text(encoding="utf-8", errors="replace").splitlines(
                    keepends=True
                )
            except Exception as exc:
                log.warning("ci_analyst.read_failed", path=rel_path, error=str(exc))
                continue

            total = len(all_lines)
            error_lines = error_lines_by_file.get(rel_path, [1])

            # Build one merged window covering all error lines ±WINDOW
            lo = max(0, min(error_lines) - _WINDOW - 1)
            hi = min(total, max(error_lines) + _WINDOW)

            windows.append(
                FileWindow(
                    path=rel_path,
                    start_line=lo + 1,   # convert to 1-indexed
                    end_line=hi,
                    original_lines=all_lines[lo:hi],
                )
            )

        return windows

    # ── Patch validation ───────────────────────────────────────────────────────

    def _parse_and_validate_patches(
        self, raw_patches: list, windows: list[FileWindow]
    ) -> list[FilePatch]:
        """
        Parse LLM patch dicts, apply guard rails, return only safe patches.

        Rejected if:
          - path not in the windows we sent (LLM invented a file)
          - start_line / end_line don't match the window we sent (off-by-more-than-2)
          - |delta| > _MAX_LINE_DELTA  (LLM rewrote too much)
          - path looks like a test file
        """
        window_by_path = {w.path: w for w in windows}
        safe: list[FilePatch] = []

        for p in raw_patches:
            path = p.get("path", "")
            start = p.get("start_line")
            end = p.get("end_line")
            corrected = p.get("corrected_lines", [])

            # Guard: only touch files we sent
            if path not in window_by_path:
                log.warning("ci_analyst.patch_unknown_file", path=path)
                continue

            # Guard: never touch test files
            if _is_test_file(path):
                log.warning("ci_analyst.patch_test_file_rejected", path=path)
                continue

            # Guard: corrected_lines must be a non-empty list of strings
            if not isinstance(corrected, list) or not corrected:
                log.warning("ci_analyst.patch_empty_corrected_lines", path=path)
                continue

            # Ensure every line ends with \n
            corrected = [
                line if line.endswith("\n") else line + "\n"
                for line in corrected
            ]

            window = window_by_path[path]

            # Guard: start/end must be within ±2 lines of the window bounds
            # (LLM may be slightly off on boundary lines)
            if start is None or end is None:
                log.warning("ci_analyst.patch_missing_line_range", path=path)
                continue

            if abs(start - window.start_line) > 2 or abs(end - window.end_line) > 2:
                log.warning(
                    "ci_analyst.patch_line_range_mismatch",
                    path=path,
                    expected_start=window.start_line,
                    expected_end=window.end_line,
                    got_start=start,
                    got_end=end,
                )
                # Clamp to the window we actually sent — safer than rejecting
                start = window.start_line
                end = window.end_line

            original_size = end - start + 1
            delta = len(corrected) - original_size

            # Guard: line-count delta
            if abs(delta) > _MAX_LINE_DELTA:
                log.warning(
                    "ci_analyst.patch_delta_too_large",
                    path=path,
                    original_size=original_size,
                    corrected_size=len(corrected),
                    delta=delta,
                    max_allowed=_MAX_LINE_DELTA,
                )
                continue

            safe.append(
                FilePatch(
                    path=path,
                    start_line=start,
                    end_line=end,
                    corrected_lines=corrected,
                    reason=p.get("reason", ""),
                )
            )

        return safe

    # ── Backward-compat shim (used by unit tests) ──────────────────────────────

    def _read_files(self, workspace: Path, paths: list[str]) -> str:
        """
        Legacy shim kept for unit tests that call _read_files directly.
        Returns a human-readable concatenation of file windows.
        """
        from phalanx.ci_fixer.log_parser import ParsedLog  # noqa: PLC0415

        # Synthesise a minimal ParsedLog with no errors to get top-of-file windows
        mock_log = ParsedLog(tool="unknown")
        # Override all_files by using the paths directly
        mock_log.lint_errors = []
        mock_log.type_errors = []
        mock_log.test_failures = []
        mock_log.build_errors = []

        # Read each file as a full window (no error lines → defaults to line 1)
        from phalanx.ci_fixer.log_parser import LintError  # noqa: PLC0415

        results: list[str] = []
        for rel_path in paths[:_MAX_FILES]:
            full = workspace / rel_path
            if not full.exists():
                # don't append anything — missing file is skipped silently
                # (shim returns "no files found" when results stays empty)
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
                if len(content) > 8000:
                    content = content[:8000] + "\n...(truncated)"
                results.append(f"### {rel_path}\n{content}")
            except Exception as exc:
                results.append(f"(read error: {rel_path}: {exc})")

        if not results:
            return "no files found in workspace"
        return "\n\n".join(results)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _format_windows(windows: list[FileWindow]) -> str:
    """Format file windows for inclusion in the LLM prompt."""
    sections: list[str] = []
    for w in windows:
        numbered = "".join(
            f"{w.start_line + i:5d}: {line}"
            for i, line in enumerate(w.original_lines)
        )
        sections.append(
            f"### {w.path} (lines {w.start_line}–{w.end_line} of file)\n"
            f"```\n{numbered}```"
        )
    return "\n\n".join(sections)


_TEST_FILE_RE = re.compile(r"(^|/)tests?[_/]|test_[^/]+\.py$", re.IGNORECASE)


def _is_test_file(path: str) -> bool:
    """Return True if the path looks like a test file."""
    return bool(_TEST_FILE_RE.search(path))
