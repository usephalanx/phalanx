"""Tests that the main-agent and coder tool-scope lists match spec §4.6."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.coder_subagent import ALLOWED_CODER_TOOLS
from phalanx.ci_fixer_v2.tool_scopes import (
    MAIN_AGENT_TOOL_NAMES,
    coder_subagent_tool_schemas,
    main_agent_tool_schemas,
)
from phalanx.ci_fixer_v2.tools import base as tools_base


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


def test_main_agent_tool_names_locked_to_spec():
    # Spec §4.6 table — lock this down. A change here must update the spec.
    assert MAIN_AGENT_TOOL_NAMES == {
        "fetch_ci_log",
        "get_pr_context",
        "get_pr_diff",
        "get_ci_history",
        "git_blame",
        "query_fingerprint",
        "read_file",
        "grep",
        "glob",
        "run_in_sandbox",
        "delegate_to_coder",
        "commit_and_push",
        "open_fix_pr_against_author_branch",
        "comment_on_pr",
        "escalate",
    }


def test_apply_patch_is_coder_only_not_in_main_scope():
    # The whole point of the coder subagent is to keep patch application
    # off the main agent's tool list.
    assert "apply_patch" not in MAIN_AGENT_TOOL_NAMES
    assert "apply_patch" in ALLOWED_CODER_TOOLS


def test_coder_scope_is_exactly_four_tools():
    assert ALLOWED_CODER_TOOLS == {
        "read_file",
        "grep",
        "apply_patch",
        "run_in_sandbox",
    }


def test_main_agent_tool_schemas_returns_every_name_in_scope():
    schemas = main_agent_tool_schemas()
    names = {s.name for s in schemas}
    assert names == MAIN_AGENT_TOOL_NAMES


def test_coder_subagent_tool_schemas_returns_every_name_in_scope():
    schemas = coder_subagent_tool_schemas()
    names = {s.name for s in schemas}
    assert names == ALLOWED_CODER_TOOLS


def test_main_agent_tool_schemas_raise_on_missing_registration():
    # If a tool is in the scope list but not registered, we want loud
    # failure, not silent "fewer tools than expected."
    tools_base.clear_registry_for_testing()
    with pytest.raises(KeyError):
        main_agent_tool_schemas()
