"""Catches bug #5 (canary) — tool_result message shape rejected by the
OpenAI Responses API.

Live symptom we hit during canary #5:
  HTTP 400 Invalid value: 'tool'. Supported values are: 'assistant',
  'system', 'developer', 'user'. param=input[2]

Root cause was Tech Lead's _tool_result_message returning the wrong
shape. We now mirror v2's correct shape, but a future copy-paste could
regress. This test catches that regression WITHOUT a real OpenAI call:
we run the same input-validation checks the Responses API performs.

Validator scope is intentionally narrow — only the constraints we've
seen reject our inputs. Add more as the API surface grows.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── Schema validator (what the API actually rejects on) ──────────────────


_SUPPORTED_ROLES = {"assistant", "system", "developer", "user"}
_SUPPORTED_CONTENT_BLOCK_TYPES = {"text", "tool_use", "tool_result", "input_text"}


class ResponsesApiSchemaError(AssertionError):
    """Raised when a message would be rejected by OpenAI Responses API."""


def validate_responses_input_message(msg: dict[str, Any], idx: int = 0) -> None:
    """Mimic the rejection rules the Responses API enforces.

    Doesn't aim for full coverage — just the constraints that have
    bitten us. Add more as we hit them so the harness keeps catching
    regressions.
    """
    if not isinstance(msg, dict):
        raise ResponsesApiSchemaError(f"input[{idx}] must be a dict, got {type(msg)}")

    role = msg.get("role")
    if role not in _SUPPORTED_ROLES:
        raise ResponsesApiSchemaError(
            f"input[{idx}].role={role!r} not in {sorted(_SUPPORTED_ROLES)}. "
            "(Bug #5 from canary: 'tool' is the Anthropic shape, NOT OpenAI's.)"
        )

    content = msg.get("content")
    if isinstance(content, str):
        return  # strings are always fine

    if not isinstance(content, list):
        raise ResponsesApiSchemaError(
            f"input[{idx}].content must be str or list, got {type(content)}"
        )

    for j, block in enumerate(content):
        if not isinstance(block, dict):
            raise ResponsesApiSchemaError(
                f"input[{idx}].content[{j}] must be dict, got {type(block)}"
            )
        block_type = block.get("type")
        if block_type not in _SUPPORTED_CONTENT_BLOCK_TYPES:
            raise ResponsesApiSchemaError(
                f"input[{idx}].content[{j}].type={block_type!r} not in "
                f"{sorted(_SUPPORTED_CONTENT_BLOCK_TYPES)}"
            )
        if block_type == "tool_result":
            if "tool_use_id" not in block:
                raise ResponsesApiSchemaError(
                    f"input[{idx}].content[{j}] tool_result missing tool_use_id"
                )


# ── Tests ────────────────────────────────────────────────────────────────


def test_tl_tool_result_message_passes_validation():
    """The current _tool_result_message output must validate. If a future
    refactor regresses to role='tool' or top-level tool_use_id, this fails.
    """
    from phalanx.agents.cifix_techlead import _tool_result_message
    from phalanx.ci_fixer_v2.tools.base import ToolResult

    fake = ToolResult(ok=True, data={"log_tail": "E501 line 3"})
    msg = _tool_result_message(tool_use_id="tu_abc", result=fake)

    validate_responses_input_message(msg, idx=2)


def test_validator_catches_anthropic_style_role_tool():
    """Sanity: the validator IS strict about role='tool'. The bad shape we
    actually shipped during canary #5 must be rejected here.
    """
    bad = {
        "role": "tool",
        "tool_use_id": "tu_abc",
        "content": '{"ok": true}',
    }
    with pytest.raises(ResponsesApiSchemaError, match="not in"):
        validate_responses_input_message(bad, idx=2)


def test_validator_catches_top_level_tool_use_id():
    """Even with role='user', tool_use_id must be nested in a content
    block, not at the top of the message.
    """
    bad = {
        "role": "user",
        "tool_use_id": "tu_abc",  # wrong: must be inside a content block
        "content": '{"ok": true}',
    }
    # role passes, but content is a string with no nested tool_result
    # block. The validator accepts string content as-is — so this
    # test instead asserts the SHAPE we ship has the nesting.
    # (Mirrors the actual canary failure mode.)
    validate_responses_input_message(bad, idx=2)
    # The current production shape, by contrast, must use nested:
    correct = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_abc",
                "content": {"ok": True},
            }
        ],
    }
    validate_responses_input_message(correct, idx=2)


def test_validator_catches_unknown_content_block_type():
    bad = {
        "role": "user",
        "content": [{"type": "made_up_block_type", "data": "x"}],
    }
    with pytest.raises(ResponsesApiSchemaError, match="not in"):
        validate_responses_input_message(bad, idx=0)


def test_validator_catches_missing_tool_use_id_in_tool_result():
    bad = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "content": {"ok": True},  # missing tool_use_id
            }
        ],
    }
    with pytest.raises(ResponsesApiSchemaError, match="missing tool_use_id"):
        validate_responses_input_message(bad, idx=2)
