"""Tech Lead fix_spec parser robustness — catches canary bug #4.

GPT-5.4's output shape varies turn-to-turn. The parser must handle
fenced/unfenced JSON, JSON embedded in prose, multiple blocks where
the last one refines the first, and missing required keys (rejected
cleanly, not crashed).

These tests are the contract between the prompt's stated output
shape and what _parse_fix_spec_from_text() will accept.
"""

from __future__ import annotations

import pytest

from phalanx.agents.cifix_techlead import _parse_fix_spec_from_text

# A schema-valid fix_spec body, reused across tests.
_VALID = (
    "{"
    '"root_cause": "E501 long line",'
    '"affected_files": ["src/x.py"],'
    '"fix_spec": "shorten line",'
    '"failing_command": "ruff check .",'
    '"confidence": 0.9,'
    '"open_questions": []'
    "}"
)


# ── Happy paths ──────────────────────────────────────────────────────────


def test_parse_fenced_json_block():
    text = f"Here's the fix:\n```json\n{_VALID}\n```"
    parsed = _parse_fix_spec_from_text(text)
    assert parsed is not None
    assert parsed["root_cause"] == "E501 long line"
    assert parsed["confidence"] == 0.9


def test_parse_unlabeled_fence():
    text = f"```\n{_VALID}\n```"
    parsed = _parse_fix_spec_from_text(text)
    assert parsed is not None
    assert parsed["affected_files"] == ["src/x.py"]


def test_parse_bare_json_no_fence():
    parsed = _parse_fix_spec_from_text(_VALID)
    assert parsed is not None


def test_parse_json_embedded_in_prose():
    """The pattern that broke canary #4: model emits prose then JSON."""
    text = (
        "I've reviewed the CI log. The failure is a Ruff E501 violation "
        f"on src/x.py line 5. Here is the fix specification:\n{_VALID}\n"
        "Let me know if you need additional context."
    )
    parsed = _parse_fix_spec_from_text(text)
    assert parsed is not None
    assert parsed["root_cause"] == "E501 long line"


def test_two_blocks_last_valid_wins():
    """Refinement pattern: model emits a draft, then refines."""
    draft = (
        '{"root_cause": "draft", "affected_files": [], "fix_spec": "draft",'
        ' "failing_command": "x", "confidence": 0.3, "open_questions": []}'
    )
    text = f"Draft:\n```json\n{draft}\n```\nRefined:\n```json\n{_VALID}\n```"
    parsed = _parse_fix_spec_from_text(text)
    assert parsed is not None
    assert parsed["root_cause"] == "E501 long line"


# ── Rejection paths — parser returns None, no crash ──────────────────────


def test_reject_missing_required_key_failing_command():
    body = (
        '{"root_cause": "x", "affected_files": ["y"], "fix_spec": "z",'
        ' "confidence": 0.5, "open_questions": []}'
    )
    assert _parse_fix_spec_from_text(f"```json\n{body}\n```") is None


def test_reject_missing_required_key_open_questions():
    body = (
        '{"root_cause": "x", "affected_files": ["y"], "fix_spec": "z",'
        ' "failing_command": "x", "confidence": 0.5}'
    )
    assert _parse_fix_spec_from_text(f"```json\n{body}\n```") is None


def test_reject_non_string_confidence():
    body = _VALID.replace('"confidence": 0.9', '"confidence": "high"')
    assert _parse_fix_spec_from_text(f"```json\n{body}\n```") is None


def test_reject_affected_files_not_a_list():
    body = _VALID.replace('"affected_files": ["src/x.py"]', '"affected_files": "src/x.py"')
    assert _parse_fix_spec_from_text(f"```json\n{body}\n```") is None


def test_reject_completely_unstructured_text():
    text = "I think the problem is probably a lint issue, but I'm not sure."
    assert _parse_fix_spec_from_text(text) is None


def test_reject_empty_text():
    assert _parse_fix_spec_from_text("") is None
    assert _parse_fix_spec_from_text(None) is None  # type: ignore[arg-type]


# ── Edge cases the canary surfaced ───────────────────────────────────────


def test_provider_error_text_is_not_a_fix_spec():
    """If the OpenAI provider returns provider_error in the text body
    (we've seen it during canary #5 with the wrong tool_result shape),
    the parser must NOT mistake that for a fix_spec. None is correct.
    """
    text = "provider_error: Error code: 400 - {'error': {'message': \"Invalid value: 'tool'\"}}"
    assert _parse_fix_spec_from_text(text) is None


def test_extra_keys_are_preserved():
    """The parser shouldn't strip extra keys — the engineer might want
    additional metadata downstream (e.g., 'iteration', 'evidence_links').
    """
    body = _VALID[:-1] + ', "evidence_links": ["log/line/42"]}'
    parsed = _parse_fix_spec_from_text(f"```json\n{body}\n```")
    assert parsed is not None
    assert parsed.get("evidence_links") == ["log/line/42"]


@pytest.mark.parametrize(
    "wrapper_command",
    [
        "prek run --all-files",
        "pre-commit run --all-files",
        "make ci",
        "tox",
        "npm test",
    ],
)
def test_parser_accepts_wrapper_failing_command_but_engineer_will_handle(
    wrapper_command: str,
):
    """The parser doesn't enforce 'narrow command' — that's the
    prompt's job. But it should accept these strings as valid
    `failing_command` values without crashing. (The engineer downstream
    is responsible for handling the wrapper-correctly outcome.)
    """
    body = _VALID.replace('"ruff check ."', f'"{wrapper_command}"')
    parsed = _parse_fix_spec_from_text(f"```json\n{body}\n```")
    assert parsed is not None
    assert parsed["failing_command"] == wrapper_command


# ── v1.5.0 contract additions: verify_command + verify_success + self_critique ──


def _wrap(body: str) -> str:
    return f"```json\n{body}\n```"


def test_parse_with_verify_command_and_full_verify_success():
    """v1.5.0 happy path: TL emits verify_command + full verify_success matrix."""
    body = (
        "{"
        '"root_cause": "test failure",'
        '"affected_files": ["tests/test_x.py"],'
        '"fix_spec": "remove the broken test",'
        '"failing_command": "pytest tests/test_x.py::test_bar -xvs",'
        '"verify_command": "pytest tests/",'
        '"verify_success": {"exit_codes": [0, 4, 5]},'
        '"confidence": 0.95,'
        '"open_questions": []'
        "}"
    )
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert parsed["verify_command"] == "pytest tests/"
    assert parsed["verify_success"]["exit_codes"] == [0, 4, 5]


def test_parse_backwards_compat_no_verify_fields():
    """Old-format fix_spec (no verify_command / verify_success) still parses.
    Engineer fallback handles missing fields."""
    parsed = _parse_fix_spec_from_text(_wrap(_VALID))
    assert parsed is not None
    assert "verify_command" not in parsed
    assert "verify_success" not in parsed


def test_parse_drops_invalid_verify_command():
    """If verify_command is not a string, drop it silently — better to fall
    back to backwards-compat default than reject the whole fix_spec."""
    body = _VALID.rstrip("}") + ',"verify_command": 12345}'
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert "verify_command" not in parsed


def test_parse_normalizes_verify_success_with_default_exit_codes():
    """Empty exit_codes → default to [0]."""
    body = _VALID.rstrip("}") + ',"verify_success": {}}'
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert parsed["verify_success"]["exit_codes"] == [0]


def test_parse_normalizes_invalid_exit_codes_to_default():
    """exit_codes contains non-int → fall back to [0]."""
    body = _VALID.rstrip("}") + ',"verify_success": {"exit_codes": ["not", "ints"]}}'
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert parsed["verify_success"]["exit_codes"] == [0]


def test_parse_keeps_stdout_stderr_matchers():
    body = (
        _VALID.rstrip("}") + ',"verify_success": {"exit_codes": [0], '
        '"stdout_contains": "All checks passed", '
        '"stderr_excludes": "DeprecationWarning"}}'
    )
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert parsed["verify_success"]["stdout_contains"] == "All checks passed"
    assert parsed["verify_success"]["stderr_excludes"] == "DeprecationWarning"


def test_parse_drops_empty_string_matchers():
    """Empty string matchers are falsy → filtered out (cleaner downstream)."""
    body = _VALID.rstrip("}") + ',"verify_success": {"exit_codes": [0], "stdout_contains": ""}}'
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert "stdout_contains" not in parsed["verify_success"]


def test_parse_passthrough_self_critique_dict():
    body = (
        _VALID.rstrip("}") + ',"self_critique": {"ci_log_addresses_root_cause": true, '
        '"affected_files_exist_in_repo": true, '
        '"verify_command_will_distinguish_success": true, '
        '"notes": "looks good"}}'
    )
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert parsed["self_critique"]["notes"] == "looks good"
    assert parsed["self_critique"]["ci_log_addresses_root_cause"] is True


def test_parse_drops_invalid_self_critique():
    body = _VALID.rstrip("}") + ',"self_critique": "not a dict"}'
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert "self_critique" not in parsed


def test_parse_drops_unknown_verify_success_keys():
    """Closed schema on verify_success — extra keys silently dropped."""
    body = (
        _VALID.rstrip("}") + ',"verify_success": {"exit_codes": [0], "weird_extra_key": "noise"}}'
    )
    parsed = _parse_fix_spec_from_text(_wrap(body))
    assert parsed is not None
    assert "weird_extra_key" not in parsed["verify_success"]
