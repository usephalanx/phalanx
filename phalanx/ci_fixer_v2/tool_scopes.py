"""Per-agent tool allow-lists.

Spec §4.6 pins which tools the main agent can see vs the coder subagent.
Enforcement lives in two places:

  1. The LLM's tool schema list — whatever we don't send to the provider,
     the provider's model can't call. This is the first defence.
  2. The coder subagent loop (coder_subagent.ALLOWED_CODER_TOOLS) re-
     checks tool names at dispatch time so a buggy main prompt can't
     smuggle a banned call through.

Keep MAIN_AGENT_TOOL_NAMES and ALLOWED_CODER_TOOLS in sync with the spec.
"""

from __future__ import annotations

from phalanx.ci_fixer_v2.coder_subagent import ALLOWED_CODER_TOOLS  # noqa: F401
from phalanx.ci_fixer_v2.tools import base as tools_base
from phalanx.ci_fixer_v2.tools.base import ToolSchema

MAIN_AGENT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # Diagnosis
        "fetch_ci_log",
        "get_pr_context",
        "get_pr_diff",
        "get_ci_history",
        "git_blame",
        "query_fingerprint",
        # Reading
        "read_file",
        "grep",
        "glob",
        # Action (main-agent-only)
        "run_in_sandbox",
        "delegate_to_coder",
        "commit_and_push",
        "open_fix_pr_against_author_branch",
        "comment_on_pr",
        "escalate",
        # NOTE: apply_patch is NOT here — only the coder subagent may call it.
    }
)


def main_agent_tool_schemas() -> list[ToolSchema]:
    """Resolve the main agent's tool schemas from the registry.

    Call this AFTER `_register_builtin_tools()` has run (e.g., after
    importing `phalanx.ci_fixer_v2.tools`). Missing tools raise KeyError
    so a stale scope list fails loudly rather than silently sending the
    LLM fewer tools than intended.
    """
    return [tools_base.get(name).schema for name in sorted(MAIN_AGENT_TOOL_NAMES)]


def coder_subagent_tool_schemas() -> list[ToolSchema]:
    """Resolve the coder subagent's tool schemas from the registry."""
    return [tools_base.get(name).schema for name in sorted(ALLOWED_CODER_TOOLS)]
