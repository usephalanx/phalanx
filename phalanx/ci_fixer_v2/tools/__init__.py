"""CI Fixer v2 tool catalog.

Each tool is a deterministic callable (no LLM inside) that the main agent
or the coder subagent may invoke. Public tool exports land here as they
are implemented. Importing this package triggers tool registration via
each implementation module's `register()` call at import time.
"""

from phalanx.ci_fixer_v2.tools import base  # re-export for `tools.base` access


def _register_builtin_tools() -> None:
    """Register every builtin tool into the module-level registry.

    Works whether it's the first call (tool module bodies ran once and
    self-registered) or a subsequent call after `clear_registry_for_testing`
    (module bodies won't re-run, so we re-register the tool objects
    explicitly here). The registry is keyed by tool name so re-registration
    of the same object is a safe overwrite.
    """
    from phalanx.ci_fixer_v2.tools import action, coder, diagnosis, reading

    base.register(diagnosis._fetch_ci_log_tool)
    base.register(diagnosis._get_pr_context_tool)
    base.register(diagnosis._get_pr_diff_tool)
    base.register(diagnosis._query_fingerprint_tool)
    base.register(diagnosis._get_ci_history_tool)
    base.register(diagnosis._git_blame_tool)
    base.register(reading._read_file_tool)
    base.register(reading._grep_tool)
    base.register(reading._glob_tool)
    base.register(action._run_in_sandbox_tool)
    base.register(action._comment_on_pr_tool)
    base.register(action._open_fix_pr_tool)
    base.register(action._escalate_tool)
    base.register(action._commit_and_push_tool)
    base.register(coder._apply_patch_tool)
    base.register(coder._replace_in_file_tool)
    base.register(coder._delegate_to_coder_tool)


_register_builtin_tools()


__all__ = ["base"]
