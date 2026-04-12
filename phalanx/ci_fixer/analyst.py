"""
CI Root Cause Analyst — LLM confirmation of structured errors.

Takes the deterministically-parsed errors from LogParser and:
1. Reads the exact file content at the flagged lines
2. Asks the LLM to confirm root cause and produce a precise fix plan
3. Returns a structured FixPlan the planner and builder can act on

The LLM here is a CONFIRMATION step — it works from structured facts
(file, line, code, message) not from raw log guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.log_parser import ParsedLog

log = structlog.get_logger(__name__)

# Max chars per file to include in the prompt
_MAX_FILE_CHARS = 4000
# Max files to read
_MAX_FILES = 6


# ── Output types ───────────────────────────────────────────────────────────────


@dataclass
class FilePatch:
    """A single file change in the fix plan."""

    path: str
    """Relative path from repo root"""

    content: str
    """Full corrected file content"""

    reason: str = ""
    """Why this file needs to change"""


@dataclass
class FixPlan:
    """
    Structured fix plan produced by the analyst.

    High confidence = builder applies it directly.
    Medium confidence = builder applies it but QA must pass first.
    Low confidence = do not commit, log for human review.
    """

    confidence: str  # "high" | "medium" | "low"
    root_cause: str
    patches: list[FilePatch] = field(default_factory=list)
    needs_new_test: bool = False
    """True when the fix requires adding a new test (not modifying existing ones)."""

    @property
    def is_actionable(self) -> bool:
        return self.confidence in ("high", "medium") and bool(self.patches)


# ── Prompt ─────────────────────────────────────────────────────────────────────

_ANALYST_PROMPT = """\
You are a senior engineer doing code review on a CI failure.

The CI system has detected the following structured errors (parsed deterministically — these are exact):

{structured_errors}

FAILING FILES (content at the exact lines flagged):
{file_contents}

Your task:
1. Confirm the root cause in one sentence.
2. Produce the minimal fix — change only what is broken.
3. NEVER modify test assertions. If a test is failing, fix the implementation.
4. For lint (F401 unused import, E501 line too long, etc.) — apply the mechanical fix.
5. For type errors — fix the type annotation or the value, whichever is correct.
6. For F404 (__future__ import not at beginning) — move the __future__ import to line 1.

Respond with ONLY valid JSON, nothing else. No markdown, no explanation outside the JSON.

{{
  "confidence": "high",
  "root_cause": "<one sentence — what is broken and why>",
  "patches": [
    {{
      "path": "<relative file path from repo root>",
      "content": "<complete corrected file content>",
      "reason": "<one sentence — what was changed>"
    }}
  ],
  "needs_new_test": false
}}

If you cannot determine a high or medium confidence fix, respond with:
{{"confidence": "low", "root_cause": "<why you cannot fix this>", "patches": [], "needs_new_test": false}}
"""


# ── Analyst class ──────────────────────────────────────────────────────────────


class RootCauseAnalyst:
    """
    Wraps the LLM call for root cause analysis.

    Takes a ParsedLog + workspace path, reads the failing files,
    and returns a FixPlan.
    """

    def __init__(self, call_llm):
        """
        Args:
            call_llm: callable(messages, system, max_tokens) -> str
                      This is typically BaseAgent._call_claude bound to the agent.
        """
        self._call_llm = call_llm

    def analyze(self, parsed_log: ParsedLog, workspace: Path) -> FixPlan:
        """
        Synchronous analysis — reads files, calls LLM, returns FixPlan.

        Kept sync because _call_claude is sync (wraps the Anthropic SDK).
        """
        if not parsed_log.has_errors:
            return FixPlan(
                confidence="low",
                root_cause="No structured errors found in the log",
            )

        file_contents = self._read_files(workspace, parsed_log.all_files)
        structured_errors = parsed_log.as_text()

        prompt = _ANALYST_PROMPT.format(
            structured_errors=structured_errors,
            file_contents=file_contents,
        )

        try:
            raw = self._call_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
            )
            raw = raw.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            data = json.loads(raw)
            patches = [
                FilePatch(
                    path=p["path"],
                    content=p["content"],
                    reason=p.get("reason", ""),
                )
                for p in data.get("patches", [])
            ]
            return FixPlan(
                confidence=data.get("confidence", "low"),
                root_cause=data.get("root_cause", ""),
                patches=patches,
                needs_new_test=data.get("needs_new_test", False),
            )

        except json.JSONDecodeError as exc:
            log.warning("ci_analyst.parse_failed", error=str(exc), raw=raw[:300])
            return FixPlan(confidence="low", root_cause="LLM returned non-JSON response")
        except Exception as exc:
            log.warning("ci_analyst.failed", error=str(exc))
            return FixPlan(confidence="low", root_cause=f"Analyst error: {exc}")

    def _read_files(self, workspace: Path, paths: list[str]) -> str:
        """Read file contents from workspace, formatted for the prompt."""
        sections: list[str] = []
        for rel_path in paths[:_MAX_FILES]:
            full = workspace / rel_path
            if not full.exists():
                # Try glob fallback
                matches = list(workspace.rglob(Path(rel_path).name))
                if matches:
                    full = matches[0]
                    rel_path = str(full.relative_to(workspace))
                else:
                    log.debug("ci_analyst.file_not_found", path=rel_path)
                    continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
                if len(content) > _MAX_FILE_CHARS:
                    content = content[:_MAX_FILE_CHARS] + "\n... (truncated)"
                sections.append(f"### {rel_path}\n```python\n{content}\n```")
            except Exception as exc:
                log.warning("ci_analyst.read_failed", path=rel_path, error=str(exc))
        return "\n\n".join(sections) if sections else "(no files found in workspace)"
