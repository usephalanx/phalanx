"""AgentContext — the shared state that flows through the main loop.

This is the *only* mutable state the loop touches. Tool results, LLM
responses, the verification flag, and cost tracking all live here. The
loop passes this object to each tool-execution hop; it is never
serialized to disk until run-finalization (when its decision timeline
and cost breakdown are written to CIFixRun).

Design rules:
  - Immutable identifiers (run_id, workspace path, failing command) are
    set at construction and never mutated.
  - Mutable state is append-only where possible (messages, decision
    timeline). last_sandbox_verified is the one clear exception — it is
    a hard gate flag; see `AgentContext.mark_sandbox_verified`.
  - No AgentContext method may call an LLM. Keep side-effect surfaces in
    the loop, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolInvocation:
    """One tool call + its result, captured for the decision timeline."""

    turn: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class CostRecord:
    """Per-provider cost accumulator — serialized to
    CIFixRun.cost_breakdown_json at run end.
    """

    gpt_reasoning_input_tokens: int = 0
    gpt_reasoning_output_tokens: int = 0
    gpt_reasoning_thinking_tokens: int = 0
    gpt_reasoning_cost_usd: float = 0.0

    sonnet_coder_input_tokens: int = 0
    sonnet_coder_output_tokens: int = 0
    sonnet_coder_thinking_tokens: int = 0
    sonnet_coder_cost_usd: float = 0.0

    sandbox_runtime_seconds: float = 0.0

    @property
    def total_cost_usd(self) -> float:
        return self.gpt_reasoning_cost_usd + self.sonnet_coder_cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "gpt_reasoning": {
                "input_tokens": self.gpt_reasoning_input_tokens,
                "output_tokens": self.gpt_reasoning_output_tokens,
                "reasoning_tokens": self.gpt_reasoning_thinking_tokens,
                "cost_usd": round(self.gpt_reasoning_cost_usd, 4),
            },
            "sonnet_coder": {
                "input_tokens": self.sonnet_coder_input_tokens,
                "output_tokens": self.sonnet_coder_output_tokens,
                "thinking_tokens": self.sonnet_coder_thinking_tokens,
                "cost_usd": round(self.sonnet_coder_cost_usd, 4),
            },
            "sandbox_runtime_seconds": round(self.sandbox_runtime_seconds, 2),
            "total_cost_usd": round(self.total_cost_usd, 4),
        }


@dataclass
class AgentContext:
    """Mutable state held by the main loop for one v2 run.

    Constructed once at the start of `run_ci_fix_v2` and passed through
    every tool hop. Never shared across runs.
    """

    # ── Immutable identifiers ──────────────────────────────────────────────
    ci_fix_run_id: str
    repo_full_name: str
    repo_workspace_path: str
    original_failing_command: str
    """The exact command that failed in CI. Sandbox verification is only
    counted when this command (or a strict superset) runs green."""

    pr_number: int | None = None
    has_write_permission: bool = False

    # ── External-dep handles (set by run bootstrap, read by tools) ─────────
    ci_api_key: str | None = None
    """CI provider token used by fetch_ci_log. Resolved at run start from
    the CIIntegration row (or settings.github_token fallback)."""

    sandbox_container_id: str | None = None
    """Docker container id of the provisioned sandbox. Set by the run
    bootstrap after SandboxProvisioner.provision() succeeds. None means
    the sandbox is not available → sandbox-dependent tools return an
    error result (per spec §N3 — no local fallback)."""

    ci_provider: str = "github_actions"
    """CI provider identifier ('github_actions', 'circleci', 'buildkite').
    Seeded from CIFixRun.ci_provider."""

    fingerprint_hash: str | None = None
    """sha256[:16] stable identity of the current failure class. Seeded from
    CIFixRun.fingerprint_hash by the run bootstrap. Used by query_fingerprint
    as the default when the agent does not supply one in the tool input."""

    author_head_branch: str | None = None
    """The author's PR head branch name (e.g. 'feature/add-auth'). Seeded
    by the run bootstrap from the triggering PR payload. Required by the
    commit_and_push tool's 'author_branch' strategy; None means
    commit_and_push must use the 'fix_branch' strategy instead."""

    # ── Append-only conversation + telemetry ───────────────────────────────
    messages: list[dict[str, Any]] = field(default_factory=list)
    """LLM conversation history — user/assistant/tool turns."""

    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    """Ordered record of every tool call + result. Feeds the decision
    timeline written to CIFixRun at finalization."""

    # ── v1.5.0 verify contract (TL → Engineer hand-off) ──────────────────
    verify_success_criteria: dict[str, Any] | None = None
    """v1.5.0 contract. Set by the engineer before invoking the coder
    subagent. Matches `fix_spec.verify_success` from TL's output:
        {"exit_codes": [int, ...],
         "stdout_contains": str | None,
         "stderr_excludes": str | None}
    None = backwards-compat default (exit_code == 0 alone gates verify).
    See docs/ci-fixer-v3-agent-contracts.md."""

    # ── Mutable gates + diagnostics ────────────────────────────────────────
    last_sandbox_verified: bool = False
    """Hard gate. True iff the most recent sandbox run covered the
    original failing command AND its exit code/stdout/stderr satisfied
    the verify_success_criteria (or, in backwards-compat mode, exit_code
    was 0). Cleared by any patch write."""

    last_attempted_diff: str | None = None
    """Most recent diff the coder subagent produced (verified or not).
    Included in escalation payloads so humans can see what we tried."""

    cost: CostRecord = field(default_factory=CostRecord)

    # ── Mutation helpers (gate-enforcing) ──────────────────────────────────
    def mark_sandbox_verified(self, command_run: str) -> bool:
        """Flip the verification gate — only if `command_run` covers the
        original failing command. Returns the resulting flag value.
        """
        if self._command_covers_original(command_run):
            self.last_sandbox_verified = True
        return self.last_sandbox_verified

    def invalidate_sandbox_verification(self) -> None:
        """Called when the workspace changes (new patch applied) — the
        previous verification no longer implies current state is green.
        """
        self.last_sandbox_verified = False

    def _command_covers_original(self, command_run: str) -> bool:
        """Conservative match: command_run contains the original failing
        command verbatim, OR is identical to it. Strict by design — we
        do not want to accidentally credit a partial re-run.
        """
        a = command_run.strip()
        b = self.original_failing_command.strip()
        return bool(a) and bool(b) and (a == b or b in a)
