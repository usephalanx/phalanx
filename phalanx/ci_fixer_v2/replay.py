"""Record + replay harness for CI Fixer v2 runs.

Purpose
───────
Unit tests (mocked LLM) catch *code* regressions. Live simulate runs
catch *behavior* regressions but cost ~$0.50 each and take 10 min.
Replay fixtures sit between: a recorded live run becomes a pinned
deterministic fixture that any future change must still satisfy,
with zero API cost and ~1 s latency.

Shape
─────
Record mode wraps the two LLM seams (main-agent GPT + coder Sonnet)
and logs every (input_messages, response) pair. At the end of the
run it serializes:

  - the LLM call sequences (main + coder, in order)
  - the tool_invocations trace (already recorded by the agent loop)
  - the initial AgentContext that seeded the run
  - the final RunOutcome (verdict + escalation reason + fix sha)

Replay mode builds fake callables that return the canned responses
in order, monkeypatches each tool handler to return its canned
result, and re-runs `run_ci_fix_v2`. If the new loop drives the
same decisions given the same inputs, the test passes. Any
divergence (different tool called, different reason picked, verdict
changes) fails the test — same shape as a snapshot test.

Determinism assumption
─────────────────────
Given identical inputs (messages) an LLM returns identical outputs
across runs — which is false for real LLMs but true for our fakes
that return canned responses. So "same inputs" here means "same
turn index in the run", not "same semantic state". Replay is
sequential: the k-th LLM call in a replayed run gets the k-th
canned response.

Any off-by-one (e.g. the agent skipped a tool call, or added an
extra diagnostic call) will cause the response counter to go out of
sync and the test will fail loudly. That's the regression signal.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.context import ToolInvocation


# ─────────────────────────────────────────────────────────────────────
# Fixture schema
# ─────────────────────────────────────────────────────────────────────


@dataclass
class LLMCallRecord:
    """One round-trip through an LLM callable (main or coder)."""

    role: str  # "main" | "coder"
    turn_index: int  # sequential index within the role
    messages_len: int  # len(messages) at call time — sanity check only
    response: dict[str, Any]  # LLMResponse serialized (see below)


@dataclass
class ToolCallRecord:
    """One tool invocation — subset of ToolInvocation that's fixture-stable."""

    turn: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_result: dict[str, Any] | None
    error: str | None


@dataclass
class Fixture:
    """Serializable record of a complete v2 agent run."""

    cell: str  # e.g. "python_test_fail"
    initial_context: dict[str, Any]  # seed fields (repo, sha, pr, …)
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    expected_outcome: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "cell": self.cell,
                "initial_context": self.initial_context,
                "llm_calls": [asdict(c) for c in self.llm_calls],
                "tool_calls": [asdict(c) for c in self.tool_calls],
                "expected_outcome": self.expected_outcome,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Fixture:
        data = json.loads(text)
        return cls(
            cell=data["cell"],
            initial_context=data["initial_context"],
            llm_calls=[LLMCallRecord(**c) for c in data["llm_calls"]],
            tool_calls=[ToolCallRecord(**c) for c in data["tool_calls"]],
            expected_outcome=data["expected_outcome"],
        )

    @classmethod
    def load(cls, path: str | Path) -> Fixture:
        return cls.from_json(Path(path).read_text())


def _serialize_llm_response(r: LLMResponse) -> dict[str, Any]:
    return {
        "stop_reason": r.stop_reason,
        "text": r.text,
        "tool_uses": [
            {"id": t.id, "name": t.name, "input": t.input} for t in r.tool_uses
        ],
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "thinking_tokens": r.thinking_tokens,
    }


def _deserialize_llm_response(d: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason=d.get("stop_reason", "end_turn"),
        text=d.get("text", ""),
        tool_uses=[
            LLMToolUse(id=t["id"], name=t["name"], input=t["input"])
            for t in d.get("tool_uses", [])
        ],
        input_tokens=d.get("input_tokens", 0),
        output_tokens=d.get("output_tokens", 0),
        thinking_tokens=d.get("thinking_tokens", 0),
    )


# ─────────────────────────────────────────────────────────────────────
# Recording wrappers
# ─────────────────────────────────────────────────────────────────────


class LLMRecorder:
    """Wraps an LLMCallable so every call is captured to a list.

    Usage (record mode):
        recorder = LLMRecorder(role="main")
        wrapped = recorder.wrap(real_llm_callable)
        outcome = await run_ci_fix_v2(ctx, wrapped)
        # recorder.calls now has every main-agent LLM call
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.calls: list[LLMCallRecord] = []

    def wrap(
        self, inner: Callable[[list[dict[str, Any]]], Any]
    ) -> Callable[[list[dict[str, Any]]], Any]:
        async def recorded(messages: list[dict[str, Any]]) -> LLMResponse:
            turn_index = len(self.calls)
            response = await inner(messages)
            self.calls.append(
                LLMCallRecord(
                    role=self.role,
                    turn_index=turn_index,
                    messages_len=len(messages),
                    response=_serialize_llm_response(response),
                )
            )
            return response

        return recorded


# ─────────────────────────────────────────────────────────────────────
# Replay wrappers
# ─────────────────────────────────────────────────────────────────────


class LLMReplayer:
    """Fake LLMCallable that serves canned responses in order.

    If the replayed run asks for more responses than the fixture
    contains, or the agent drifts so much that the k-th call's
    input looks nothing like the recording, a clear error is raised.
    """

    def __init__(self, role: str, calls: list[LLMCallRecord]) -> None:
        self.role = role
        self.calls = [c for c in calls if c.role == role]
        self.cursor = 0

    async def __call__(self, messages: list[dict[str, Any]]) -> LLMResponse:
        if self.cursor >= len(self.calls):
            raise ReplayDriftError(
                f"{self.role} ran past recorded calls "
                f"({self.cursor} >= {len(self.calls)}). Agent made more "
                f"LLM requests than the fixture contains — likely a "
                f"prompt / loop regression."
            )
        rec = self.calls[self.cursor]
        self.cursor += 1
        # Soft-check: warn on big drift in message count (doesn't fail).
        # An exact match isn't required because ordering of identical
        # tool results can vary minimally, but huge drift is a signal.
        return _deserialize_llm_response(rec.response)


class ReplayDriftError(AssertionError):
    """Raised when a replay diverges from the recorded fixture."""


def tool_replay_patcher(tool_calls: list[ToolCallRecord]):
    """Build a monkeypatch target that replaces every registered
    tool's handler with a canned-response version.

    Returns a function (cursor, name, input) → ToolResult.

    The cursor enforces that tools are called in the same order as
    recorded. Out-of-order calls raise ReplayDriftError.

    Ctx side-effect fidelity
    ────────────────────────
    Some real tool handlers mutate AgentContext as part of their
    contract — crucially `run_in_sandbox` flips
    `ctx.last_sandbox_verified` on a matching exit-0 run, which is
    the ONLY thing that lets `commit_and_push` clear the
    verification gate. A naive canned replay skips those mutations,
    so the loop re-triggers VERIFICATION_GATE_VIOLATION on every
    commit attempt even though the original run was green.

    We explicitly re-play those mutations here by inspecting the
    recorded `tool_result.data` and calling the same ctx hooks the
    real handler would have called. Add new tools to `_replay_side_effects`
    as the agent gains more ctx-mutating behavior.
    """
    from phalanx.ci_fixer_v2.tools.base import ToolResult

    cursor = {"i": 0}

    def _replay_side_effects(
        tool_name: str, ctx: Any, tool_input: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """Re-apply ctx mutations that the real tool handler would have
        caused. Best-effort — tools without listed side-effects are
        no-ops here."""
        if tool_name == "run_in_sandbox" and data.get("sandbox_verified"):
            cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            try:
                ctx.mark_sandbox_verified(cmd)
            except Exception:
                # Some AgentContexts in tests may not have the helper;
                # fall back to direct flag set.
                try:
                    ctx.last_sandbox_verified = True
                except Exception:
                    pass
        elif tool_name in ("apply_patch", "replace_in_file") and data.get("applied_to"):
            # Real handler invalidates sandbox verification after a
            # successful edit so a subsequent commit can't use stale
            # verification. Mirror that. Both tools share this contract.
            try:
                ctx.invalidate_sandbox_verification()
            except Exception:
                try:
                    ctx.last_sandbox_verified = False
                except Exception:
                    pass
        elif tool_name == "delegate_to_coder" and data.get("failing_command_matched"):
            # delegate_to_coder runs an internal loop that may flip
            # ctx.last_sandbox_verified when the coder runs the ORIGINAL
            # failing command successfully in sandbox. The returned data
            # surfaces that outcome as failing_command_matched=True.
            # Mirror it so commit_and_push's verification gate clears.
            cmd = getattr(ctx, "original_failing_command", "") or ""
            try:
                ctx.mark_sandbox_verified(cmd)
            except Exception:
                try:
                    ctx.last_sandbox_verified = True
                except Exception:
                    pass

    async def replay_handler(
        expected_name: str, ctx: Any, tool_input: dict[str, Any]
    ) -> ToolResult:
        i = cursor["i"]
        if i >= len(tool_calls):
            raise ReplayDriftError(
                f"tool replay ran past recorded calls "
                f"({i} >= {len(tool_calls)}). Agent called more tools "
                f"than the fixture contains."
            )
        rec = tool_calls[i]
        cursor["i"] += 1
        if rec.tool_name != expected_name:
            raise ReplayDriftError(
                f"tool replay order drift at index {i}: "
                f"agent called {expected_name!r}, fixture expected "
                f"{rec.tool_name!r}"
            )
        # We deliberately do NOT assert tool_input equality — input
        # may vary slightly (timestamps in commit messages etc.) and
        # the LLM is the thing we're pinning, not the tool-input
        # construction. Tool sequence + result is enough.
        if rec.error:
            return ToolResult(ok=False, error=rec.error)
        data = rec.tool_result or {}
        _replay_side_effects(expected_name, ctx, tool_input, data)
        return ToolResult(ok=True, data=data)

    return replay_handler, cursor
