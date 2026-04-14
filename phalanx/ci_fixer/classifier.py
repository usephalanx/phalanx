"""
CI failure classifier — categorizes a CI log into a failure type.

Two layers:
  1. classify_failure()  — fast regex, no LLM, used as webhook pre-filter
  2. LLMClassifier       — GPT-4.1 structured JSON, used before repair agent

LLMClassifier produces a ClassificationResult with:
  - failure_type, language, tool: what kind of failure and with what tool
  - complexity_tier (L1/L2/L3): how hard the fix is
  - confidence: 0.0–1.0
  - root_cause_hypothesis: one grounded sentence
  - affected_symbols: error codes and symbol names from the log
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.log_parser import ParsedLog

log = structlog.get_logger(__name__)

# ── Types ──────────────────────────────────────────────────────────────────────

FailureType = Literal["lint", "type", "test", "build", "dependency", "unknown"]
Language = Literal["python", "typescript", "javascript", "java", "rust", "go", "unknown"]
ToolName = Literal[
    "ruff", "mypy", "pytest", "eslint", "tsc",
    "maven", "cargo", "jest", "docker", "unknown",
]
ComplexityTier = Literal["L1", "L2", "L3"]

# L1-eligible lint codes — single-line mechanical fixes only
_L1_CODES: frozenset[str] = frozenset({
    "F401", "E501", "W291", "W293", "W292", "I001", "F811",
})

# ── ClassificationResult ───────────────────────────────────────────────────────


@dataclass
class ClassificationResult:
    """
    Structured output of the LLM classifier.
    All values are validated/clamped in __post_init__.
    """

    failure_type: FailureType
    language: Language
    tool: ToolName
    complexity_tier: ComplexityTier
    confidence: float                        # 0.0–1.0
    root_cause_hypothesis: str               # one sentence grounded in log
    affected_symbols: list[str] = field(default_factory=list)  # max 10

    def __post_init__(self) -> None:
        # Clamp confidence
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        # Cap symbols
        self.affected_symbols = list(self.affected_symbols[:10])
        # L1 is only valid for lint failures
        if self.failure_type != "lint" and self.complexity_tier == "L1":
            self.complexity_tier = "L2"
        # Dependencies always need L3 (cross-file, lockfile changes)
        if self.failure_type == "dependency":
            self.complexity_tier = "L3"

    @property
    def is_l1(self) -> bool:
        return self.complexity_tier == "L1"

    @property
    def is_actionable(self) -> bool:
        """False when classifier has no idea what the failure is."""
        return self.failure_type != "unknown" or self.confidence >= 0.5


# ── Classifier prompt ──────────────────────────────────────────────────────────

_CLASSIFIER_SYSTEM = (
    "You are a CI failure classifier API. "
    "Return ONLY a single JSON object matching the schema. "
    "No prose, no markdown fences, no explanation outside the JSON."
)

_CLASSIFIER_PROMPT = """\
Classify the CI failure below. Return ONLY valid JSON matching the schema at the bottom.

--- STRUCTURED ERRORS ---
{structured_errors}

--- RAW LOG EXCERPT (first 800 chars) ---
{raw_excerpt}

--- CLASSIFICATION RULES ---

failure_type:
  "lint"       → ruff, eslint, pylint — error codes like F401, E501, no-unused-vars
  "type"       → mypy errors, tsc TS#### — type mismatch, incompatible assignment
  "test"       → pytest FAILED, jest failure, AssertionError
  "build"      → SyntaxError, ModuleNotFoundError, ImportError, cargo error[E####],
                 maven compile error, docker build failure
  "dependency" → pip ResolutionImpossible, npm ERESOLVE, yarn peer dep conflict
  "unknown"    → none of the above match

language: infer from file extensions or tool name.
  .py→"python"  .ts/.tsx→"typescript"  .js/.jsx→"javascript"
  pom.xml/mvn→"java"  Cargo.toml/cargo→"rust"  go.mod/go→"go"
  default "unknown"

tool: the specific tool that reported the failure.
  "ruff"|"mypy"|"pytest"|"eslint"|"tsc"|"maven"|"cargo"|"jest"|"docker"|"unknown"

complexity_tier:
  "L1" → ALL errors are single-line mechanical fixes AND failure_type=="lint":
         F401 (remove one import line), E501 (wrap one line),
         W291/W293 (strip trailing whitespace), W292 (add newline),
         I001 (sort imports in one block), F811 (remove duplicate import)
  "L2" → fix is contained within one file/function — single test failure,
         single type error, single-file build error, mixed lint codes
  "L3" → fix requires multiple files, cross-module symbol changes,
         missing dependency install, docker/maven config change,
         type error propagating across files

confidence:
  0.9-1.0 → errors are deterministically parsed, type is unambiguous
  0.7-0.89 → type clear but complexity tier uncertain
  0.5-0.69 → noisy log or mixed failure types
  0.0-0.49 → cannot reliably classify

root_cause_hypothesis: one sentence, grounded in what the log says.
  Good: "Unused import 'os' on line 3 of phalanx/api/routes/foo.py"
  Bad:  "There might be an issue with the imports"

affected_symbols: list of error codes, function/class/variable names
  directly named in the errors. Max 10. Empty list if none.

--- OUTPUT SCHEMA ---
{{
  "failure_type": "lint"|"type"|"test"|"build"|"dependency"|"unknown",
  "language": "python"|"typescript"|"javascript"|"java"|"rust"|"go"|"unknown",
  "tool": "ruff"|"mypy"|"pytest"|"eslint"|"tsc"|"maven"|"cargo"|"jest"|"docker"|"unknown",
  "complexity_tier": "L1"|"L2"|"L3",
  "confidence": <float 0.0-1.0>,
  "root_cause_hypothesis": "<one sentence>",
  "affected_symbols": ["<string>", ...]
}}"""


# ── LLMClassifier ──────────────────────────────────────────────────────────────


class LLMClassifier:
    """
    Calls GPT-4.1 to produce a ClassificationResult from a ParsedLog.

    Falls back to _classify_from_parsed() (pure deterministic) on any
    LLM failure — never raises, always returns a ClassificationResult.
    """

    def __init__(self, model: str | None = None) -> None:
        from phalanx.config.settings import get_settings  # noqa: PLC0415
        s = get_settings()
        self._model = model or s.openai_model_reasoning  # gpt-4.1

    def classify(
        self,
        parsed_log: "ParsedLog",
        raw_log: str,
    ) -> ClassificationResult:
        """
        Classify a CI failure. Returns ClassificationResult.
        Never raises — falls back to deterministic classifier on any error.
        """
        try:
            return self._llm_classify(parsed_log, raw_log)
        except Exception as exc:
            log.warning("ci_classifier.llm_failed", error=str(exc))
            return _classify_from_parsed(parsed_log)

    def _llm_classify(
        self,
        parsed_log: "ParsedLog",
        raw_log: str,
    ) -> ClassificationResult:
        from phalanx.agents.openai_client import OpenAIClient  # noqa: PLC0415
        from phalanx.ci_fixer.log_parser import clean_log  # noqa: PLC0415

        prompt = _CLASSIFIER_PROMPT.format(
            structured_errors=parsed_log.as_text(),
            raw_excerpt=clean_log(raw_log)[:800],
        )

        client = OpenAIClient(model=self._model)
        data = client.call(
            messages=[{"role": "user", "content": prompt}],
            system=_CLASSIFIER_SYSTEM,
            max_tokens=512,
            temperature=0.0,
        )
        return _parse_classification(data)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _parse_classification(data: dict) -> ClassificationResult:
    """Parse raw JSON dict from GPT into a ClassificationResult."""
    return ClassificationResult(
        failure_type=data.get("failure_type", "unknown"),
        language=data.get("language", "unknown"),
        tool=data.get("tool", "unknown"),
        complexity_tier=data.get("complexity_tier", "L2"),
        confidence=float(data.get("confidence", 0.5)),
        root_cause_hypothesis=str(data.get("root_cause_hypothesis", "")),
        affected_symbols=list(data.get("affected_symbols", [])),
    )


def _classify_from_parsed(parsed_log: "ParsedLog") -> ClassificationResult:
    """
    Deterministic fallback — derives ClassificationResult from ParsedLog
    without any LLM call. Uses the existing classify_failure() regex
    plus ParsedLog fields for richer output.
    """
    # Determine failure_type from ParsedLog structure
    if parsed_log.lint_errors:
        failure_type: FailureType = "lint"
        tool_name: ToolName = parsed_log.tool if parsed_log.tool in ("ruff", "eslint") else "ruff"  # type: ignore[assignment]
        language: Language = "python" if parsed_log.tool == "ruff" else "unknown"  # type: ignore[assignment]
        codes = {e.code for e in parsed_log.lint_errors}
        tier: ComplexityTier = "L1" if codes.issubset(_L1_CODES) else "L2"
        symbols = sorted(codes)[:10]
        hypothesis = (
            f"{len(parsed_log.lint_errors)} {', '.join(sorted(codes)[:3])} "
            f"error(s) in {parsed_log.lint_errors[0].file}"
        )
    elif parsed_log.type_errors:
        failure_type = "type"
        tool_name = parsed_log.tool if parsed_log.tool in ("mypy", "tsc") else "mypy"  # type: ignore[assignment]
        language = "python" if parsed_log.tool == "mypy" else "typescript"  # type: ignore[assignment]
        tier = "L2"
        symbols = [e.file for e in parsed_log.type_errors[:5]]
        hypothesis = f"{len(parsed_log.type_errors)} type error(s) in {parsed_log.type_errors[0].file}"
    elif parsed_log.test_failures:
        failure_type = "test"
        tool_name = "pytest"
        language = "python"
        tier = "L2"
        symbols = [f.test_id.split("::")[-1] for f in parsed_log.test_failures[:5]]
        hypothesis = f"{len(parsed_log.test_failures)} test failure(s): {parsed_log.test_failures[0].test_id}"
    elif parsed_log.build_errors:
        failure_type = "build"
        tool_name = "unknown"
        language = "unknown"
        tier = "L2"
        symbols = []
        hypothesis = parsed_log.build_errors[0].message[:120]
    else:
        failure_type = "unknown"
        tool_name = "unknown"
        language = "unknown"
        tier = "L2"
        symbols = []
        hypothesis = "No structured errors found in log"

    return ClassificationResult(
        failure_type=failure_type,
        language=language,
        tool=tool_name,
        complexity_tier=tier,
        confidence=0.7 if failure_type != "unknown" else 0.3,
        root_cause_hypothesis=hypothesis,
        affected_symbols=symbols,
    )

# Ordered by specificity — first match wins
_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "lint",
        [
            r"ruff.*error",
            r"pylint.*error",
            r"eslint.*error",
            r"prettier.*error",
            r"Found \d+ error",
            r"error\[E\d+\]",  # ruff error codes
            r"\b[EWF]\d{3,4}\b",  # ruff/flake8: F401, E501, W291 etc.
            r"W\d{4}",  # pylint warning codes
            r"Trailing whitespace",
            r"Missing newline at end of file",
        ],
    ),
    (
        "type",
        [
            r"error TS\d+",  # tsc
            r"error: Argument of type",  # mypy
            r"error: Incompatible return value",  # mypy
            r"error: Item .* has no attribute",  # mypy
            r"error: Cannot find name",  # tsc
            r"Type '\w+' is not assignable",  # tsc
            r"Property '\w+' does not exist",  # tsc
            r"mypy.*Found \d+ error",
        ],
    ),
    (
        "test",
        [
            r"FAILED tests/",  # pytest
            r"AssertionError",
            r"assert .* ==",
            r"\d+ failed",
            r"● Test Suites:.*failed",  # jest
            r"FAIL src/",  # jest
            r"Tests:\s+\d+ failed",
            r"Expected:.*\nReceived:",
            r"test.*FAILED",
        ],
    ),
    (
        "build",
        [
            r"SyntaxError",
            r"IndentationError",
            r"ModuleNotFoundError",
            r"ImportError",
            r"Cannot find module",
            r"Module not found",
            r"Failed to compile",
            r"Build failed",
            r"error: cannot open",
            r"make.*Error \d+",
        ],
    ),
    (
        "dependency",
        [
            r"ResolutionImpossible",
            r"Could not find a version",
            r"No matching distribution",
            r"peer dep missing",
            r"Could not resolve dependency",
            r"npm ERR! code ERESOLVE",
            r"yarn error",
        ],
    ),
]


def classify_failure(log_text: str) -> str:
    """
    Return the failure category for a CI log.

    Categories (in priority order):
      lint, type, test, build, dependency, unknown

    Lint and type checks are checked before test because a lint
    failure in CI often appears alongside test output.
    """
    for category, patterns in _PATTERNS:
        if any(re.search(p, log_text, re.IGNORECASE | re.MULTILINE) for p in patterns):
            return category
    return "unknown"


def extract_failing_files(log_text: str) -> list[str]:
    """
    Extract file paths mentioned in CI failure output.

    Handles common formats:
      - pytest:  FAILED tests/unit/test_foo.py::TestBar::test_baz
      - jest:    FAIL src/components/Foo.test.tsx
      - mypy:    src/foo.py:42: error: ...
      - ruff:    src/foo.py:42:5: E501 ...
      - tsc:     src/foo.ts(42,5): error TS2345: ...
    """
    patterns = [
        r"FAILED\s+([\w/\.\-]+\.py)",  # pytest
        r"FAIL\s+([\w/\.\-]+\.[jt]sx?)",  # jest
        r"([\w/\.\-]+\.py):\d+:\d+:\s+[A-Z]\d+",  # ruff: path:line:col: CODE
        r"([\w/\.\-]+\.py):\d+:\s+error",  # mypy: path:line: error
        r"([\w/\.\-]+\.[jt]sx?)[\(:]\d+",  # tsc/eslint
        r"error in ([\w/\.\-]+\.\w+)",
    ]
    files: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, log_text, re.MULTILINE):
            path = match.group(1)
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files[:10]  # cap at 10 files to keep prompt manageable
