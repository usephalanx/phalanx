"""
Slice 1 unit tests — ClassificationResult + LLMClassifier.

All LLM responses are mocked. No API keys needed.
Proves:
  1. ClassificationResult __post_init__ guard rails
  2. LLMClassifier happy path: valid GPT JSON → correct ClassificationResult
  3. LLMClassifier fallback: GPT exception / bad JSON → deterministic result
  4. _classify_from_parsed covers all 5 ParsedLog shapes
  5. Canonical log classification for ruff, mypy, pytest, jest, cargo, docker, maven
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from phalanx.ci_fixer.classifier import (
    ClassificationResult,
    LLMClassifier,
    _classify_from_parsed,
    _parse_classification,
)
from phalanx.ci_fixer.log_parser import (
    BuildError,
    LintError,
    ParsedLog,
    TestFailure,
    TypeError,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _ruff_log() -> ParsedLog:
    p = ParsedLog(tool="ruff")
    p.lint_errors = [LintError(file="src/foo.py", line=3, col=1, code="F401", message="'os' imported but unused")]
    return p


def _mypy_log() -> ParsedLog:
    p = ParsedLog(tool="mypy")
    p.type_errors = [TypeError(file="src/foo.py", line=42, col=0, message="Incompatible return value type")]
    return p


def _pytest_log() -> ParsedLog:
    p = ParsedLog(tool="pytest")
    p.test_failures = [TestFailure(test_id="tests/unit/test_auth.py::TestLogin::test_ok", file="tests/unit/test_auth.py", message="AssertionError: expected 200 got 401")]
    return p


def _build_log() -> ParsedLog:
    p = ParsedLog(tool="build")
    p.build_errors = [BuildError(file="src/main.py", message="ModuleNotFoundError: No module named 'httpx'")]
    return p


def _empty_log() -> ParsedLog:
    return ParsedLog(tool="unknown")


# ── ClassificationResult guard rails ──────────────────────────────────────────


class TestClassificationResultGuards:
    def _make(self, **kwargs) -> ClassificationResult:
        defaults = dict(
            failure_type="lint", language="python", tool="ruff",
            complexity_tier="L1", confidence=0.9,
            root_cause_hypothesis="unused import",
        )
        defaults.update(kwargs)
        return ClassificationResult(**defaults)

    def test_confidence_clamped_above_1(self):
        r = self._make(confidence=2.5)
        assert r.confidence == 1.0

    def test_confidence_clamped_below_0(self):
        r = self._make(confidence=-0.5)
        assert r.confidence == 0.0

    def test_confidence_float_coercion(self):
        r = self._make(confidence="0.8")
        assert r.confidence == pytest.approx(0.8)

    def test_affected_symbols_capped_at_10(self):
        r = self._make(affected_symbols=[str(i) for i in range(20)])
        assert len(r.affected_symbols) == 10

    def test_l1_coerced_to_l2_for_type_failure(self):
        r = self._make(failure_type="type", complexity_tier="L1")
        assert r.complexity_tier == "L2"

    def test_l1_coerced_to_l2_for_test_failure(self):
        r = self._make(failure_type="test", complexity_tier="L1")
        assert r.complexity_tier == "L2"

    def test_l1_coerced_to_l2_for_build_failure(self):
        r = self._make(failure_type="build", complexity_tier="L1")
        assert r.complexity_tier == "L2"

    def test_dependency_always_l3(self):
        r = self._make(failure_type="dependency", complexity_tier="L1")
        assert r.complexity_tier == "L3"

    def test_dependency_l2_also_coerced(self):
        r = self._make(failure_type="dependency", complexity_tier="L2")
        assert r.complexity_tier == "L3"

    def test_l1_lint_stays_l1(self):
        r = self._make(failure_type="lint", complexity_tier="L1")
        assert r.complexity_tier == "L1"

    def test_is_l1_property(self):
        assert self._make(complexity_tier="L1").is_l1 is True
        assert self._make(complexity_tier="L2").is_l1 is False

    def test_is_actionable_known_type(self):
        assert self._make(failure_type="lint").is_actionable is True

    def test_is_actionable_unknown_low_confidence(self):
        r = self._make(failure_type="unknown", confidence=0.3)
        assert r.is_actionable is False

    def test_is_actionable_unknown_high_confidence(self):
        r = self._make(failure_type="unknown", confidence=0.6)
        assert r.is_actionable is True


# ── _parse_classification ──────────────────────────────────────────────────────


class TestParseClassification:
    def test_full_valid_dict(self):
        data = {
            "failure_type": "lint", "language": "python", "tool": "ruff",
            "complexity_tier": "L1", "confidence": 0.95,
            "root_cause_hypothesis": "unused import 'os'",
            "affected_symbols": ["F401", "os"],
        }
        r = _parse_classification(data)
        assert r.failure_type == "lint"
        assert r.language == "python"
        assert r.tool == "ruff"
        assert r.complexity_tier == "L1"
        assert r.confidence == pytest.approx(0.95)
        assert r.root_cause_hypothesis == "unused import 'os'"
        assert r.affected_symbols == ["F401", "os"]

    def test_missing_fields_get_defaults(self):
        r = _parse_classification({})
        assert r.failure_type == "unknown"
        assert r.language == "unknown"
        assert r.tool == "unknown"
        assert r.complexity_tier == "L2"
        assert r.confidence == pytest.approx(0.5)
        assert r.affected_symbols == []


# ── _classify_from_parsed deterministic fallback ───────────────────────────────


class TestClassifyFromParsed:
    def test_ruff_f401_gives_l1(self):
        r = _classify_from_parsed(_ruff_log())
        assert r.failure_type == "lint"
        assert r.tool == "ruff"
        assert r.language == "python"
        assert r.complexity_tier == "L1"
        assert r.confidence >= 0.7
        assert "F401" in r.affected_symbols

    def test_mypy_gives_type_l2(self):
        r = _classify_from_parsed(_mypy_log())
        assert r.failure_type == "type"
        assert r.tool == "mypy"
        assert r.complexity_tier == "L2"

    def test_pytest_gives_test_l2(self):
        r = _classify_from_parsed(_pytest_log())
        assert r.failure_type == "test"
        assert r.tool == "pytest"
        assert r.complexity_tier == "L2"

    def test_build_error_gives_build_l2(self):
        r = _classify_from_parsed(_build_log())
        assert r.failure_type == "build"
        assert r.complexity_tier == "L2"

    def test_empty_log_gives_unknown(self):
        r = _classify_from_parsed(_empty_log())
        assert r.failure_type == "unknown"
        assert r.confidence < 0.5
        assert r.is_actionable is False

    def test_mixed_lint_codes_non_l1_gives_l2(self):
        p = ParsedLog(tool="ruff")
        # E711 is not in L1 set → should be L2
        p.lint_errors = [
            LintError(file="src/foo.py", line=1, col=1, code="F401", message="unused"),
            LintError(file="src/foo.py", line=5, col=1, code="E711", message="comparison"),
        ]
        r = _classify_from_parsed(p)
        assert r.complexity_tier == "L2"


# ── LLMClassifier happy path ───────────────────────────────────────────────────


class TestLLMClassifierHappyPath:
    """All GPT responses mocked — no API key needed."""

    def _gpt_response(self, **kwargs) -> dict:
        return {
            "failure_type": "lint", "language": "python", "tool": "ruff",
            "complexity_tier": "L1", "confidence": 0.95,
            "root_cause_hypothesis": "unused import 'os' on line 3",
            "affected_symbols": ["F401", "os"],
            **kwargs,
        }

    def test_valid_gpt_json_produces_result(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value=self._gpt_response()):
            clf = LLMClassifier()
            result = clf.classify(_ruff_log(), raw_log="F401 error in src/foo.py")
        assert result.failure_type == "lint"
        assert result.tool == "ruff"
        assert result.complexity_tier == "L1"
        assert result.confidence == pytest.approx(0.95)

    def test_gpt_mypy_result(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value=self._gpt_response(
            failure_type="type", tool="mypy", complexity_tier="L2", confidence=0.88,
            root_cause_hypothesis="Incompatible return type in get_user()",
            affected_symbols=["get_user", "Optional[User]"],
        )):
            result = LLMClassifier().classify(_mypy_log(), raw_log="mypy error")
        assert result.failure_type == "type"
        assert result.tool == "mypy"
        assert result.complexity_tier == "L2"

    def test_gpt_cargo_result(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value=self._gpt_response(
            failure_type="build", language="rust", tool="cargo",
            complexity_tier="L2", confidence=0.9,
            root_cause_hypothesis="use of moved value in main.rs line 42",
            affected_symbols=["E0382"],
        )):
            result = LLMClassifier().classify(_build_log(), raw_log="cargo error")
        assert result.language == "rust"
        assert result.tool == "cargo"

    def test_gpt_docker_result(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value=self._gpt_response(
            failure_type="build", language="unknown", tool="docker",
            complexity_tier="L3", confidence=0.78,
            root_cause_hypothesis="COPY failed: file not found in build context",
            affected_symbols=[],
        )):
            p = ParsedLog(tool="build")
            p.build_errors = [BuildError(file=None, message="COPY failed: file not found")]
            result = LLMClassifier().classify(p, raw_log="docker build failed")
        assert result.tool == "docker"
        assert result.complexity_tier == "L3"

    def test_gpt_jest_result(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value=self._gpt_response(
            failure_type="test", language="typescript", tool="jest",
            complexity_tier="L2", confidence=0.92,
            root_cause_hypothesis="Login component test fails: expected 'Submit' got 'Loading'",
            affected_symbols=["LoginForm", "test_submit"],
        )):
            result = LLMClassifier().classify(_empty_log(), raw_log="jest failure")
        assert result.tool == "jest"
        assert result.language == "typescript"

    def test_gpt_maven_result(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value=self._gpt_response(
            failure_type="build", language="java", tool="maven",
            complexity_tier="L2", confidence=0.85,
            root_cause_hypothesis="cannot find symbol UserRepository in AuthService.java line 18",
            affected_symbols=["UserRepository", "AuthService"],
        )):
            result = LLMClassifier().classify(_empty_log(), raw_log="mvn compile")
        assert result.language == "java"
        assert result.tool == "maven"


# ── LLMClassifier fallback paths ───────────────────────────────────────────────


class TestLLMClassifierFallback:
    def test_openai_exception_falls_back_to_deterministic(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", side_effect=RuntimeError("timeout")):
            result = LLMClassifier().classify(_ruff_log(), raw_log="ruff errors")
        # Falls back — still returns something valid
        assert result.failure_type == "lint"
        assert result.confidence >= 0.5

    def test_openai_value_error_falls_back(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", side_effect=ValueError("invalid JSON")):
            result = LLMClassifier().classify(_mypy_log(), raw_log="mypy errors")
        assert result.failure_type == "type"

    def test_gpt_low_confidence_passes_through(self):
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value={
            "failure_type": "unknown", "language": "unknown", "tool": "unknown",
            "complexity_tier": "L2", "confidence": 0.2,
            "root_cause_hypothesis": "unclear",
            "affected_symbols": [],
        }):
            result = LLMClassifier().classify(_empty_log(), raw_log="gibberish")
        assert result.confidence == pytest.approx(0.2)
        assert result.is_actionable is False

    def test_gpt_invalid_tier_coerced(self):
        """GPT returns L1 for a type failure — __post_init__ coerces to L2."""
        with patch("phalanx.agents.openai_client.OpenAIClient.call", return_value={
            "failure_type": "type", "language": "python", "tool": "mypy",
            "complexity_tier": "L1", "confidence": 0.9,
            "root_cause_hypothesis": "type error",
            "affected_symbols": [],
        }):
            result = LLMClassifier().classify(_mypy_log(), raw_log="mypy")
        # __post_init__ must coerce L1→L2 for non-lint
        assert result.complexity_tier == "L2"
