"""v1.7.2.8 — TL efficiency on large files.

Production guards (always on; not opt-in):
  G1. read_file size guard
      - file ≤ 500 lines: full read OK
      - 501-1000 lines + reason="need_full_file": full read OK
      - 501-1000 lines without reason: BLOCKED
      - 1001-2000 lines + reason: BLOCKED
      - >2000 lines: NEVER full read
  G2. find_symbol locates def/class via AST (Python) or regex (other).
  G3. read_file around_line + context returns a bounded snippet.
  G4. Loop rejects repeated reads of the same (path, range_key).
  G5. Loop appends `_tl_budget` footer to every tool_result.
  G6. Loop injects force-emit-or-escalate at turn 3 and rejects further
      tool calls.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from phalanx.agents.cifix_techlead import (
    _FORCE_EMIT_AFTER_TURN,
    _append_budget_footer,
    _build_force_emit_message,
    _read_file_cache_key,
    _run_investigation_loop,
)
from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.tools.reading import (
    _FULL_READ_HARD_CEILING,
    _FULL_READ_LINE_LIMIT,
    _FULL_READ_OVERRIDE_LIMIT,
    _handle_find_symbol,
    _handle_read_file,
)


class _FakeCtx:
    """Minimal AgentContext stand-in for tool-handler tests."""

    def __init__(self, workspace: str) -> None:
        self.repo_workspace_path = workspace
        self.messages: list[dict] = []


# ── G1: read_file size guard ───────────────────────────────────────────────


class TestReadFileSizeGuard:
    def _write(self, dirpath: str, name: str, n_lines: int) -> None:
        with open(os.path.join(dirpath, name), "w") as f:
            for i in range(n_lines):
                f.write(f"x = {i}\n")

    def test_small_file_full_read_allowed(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                self._write(d, "small.py", 100)
                ctx = _FakeCtx(d)
                r = await _handle_read_file(ctx, {"path": "small.py"})
                assert r.ok, r.error
                assert r.data["line_count"] == 100

        asyncio.run(_run())

    def test_501_to_1000_lines_blocked_without_reason(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                self._write(d, "mid.py", 700)
                ctx = _FakeCtx(d)
                r = await _handle_read_file(ctx, {"path": "mid.py"})
                assert not r.ok
                assert "full_read_blocked" in r.error
                assert str(_FULL_READ_LINE_LIMIT) in r.error

        asyncio.run(_run())

    def test_501_to_1000_lines_allowed_with_reason(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                self._write(d, "mid.py", 700)
                ctx = _FakeCtx(d)
                r = await _handle_read_file(
                    ctx, {"path": "mid.py", "reason": "need_full_file"}
                )
                assert r.ok, r.error
                assert r.data["line_count"] == 700

        asyncio.run(_run())

    def test_1001_to_2000_lines_blocked_with_reason(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                self._write(d, "big.py", 1500)
                ctx = _FakeCtx(d)
                r = await _handle_read_file(
                    ctx, {"path": "big.py", "reason": "need_full_file"}
                )
                assert not r.ok
                assert "override ceiling" in r.error
                assert str(_FULL_READ_OVERRIDE_LIMIT) in r.error

        asyncio.run(_run())

    def test_over_2000_lines_never_full_read(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                self._write(d, "huge.py", 2500)
                ctx = _FakeCtx(d)

                # Without reason
                r = await _handle_read_file(ctx, {"path": "huge.py"})
                assert not r.ok
                assert "hard ceiling" in r.error

                # With reason — still blocked
                r = await _handle_read_file(
                    ctx, {"path": "huge.py", "reason": "need_full_file"}
                )
                assert not r.ok
                assert "hard ceiling" in r.error
                assert str(_FULL_READ_HARD_CEILING) in r.error

        asyncio.run(_run())

    def test_huge_file_bounded_read_via_around_line(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                self._write(d, "huge.py", 2500)
                ctx = _FakeCtx(d)
                r = await _handle_read_file(
                    ctx, {"path": "huge.py", "around_line": 1200, "context": 5}
                )
                assert r.ok, r.error
                assert r.data["line_start"] == 1195
                assert r.data["line_end"] == 1205

        asyncio.run(_run())


# ── G2: find_symbol ────────────────────────────────────────────────────────


class TestFindSymbol:
    def test_locates_function_in_2500_line_python_file(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                lines = [f"# pad {i}" for i in range(1, 1201)]
                lines.append("def naturaldate(value):")
                lines.append("    return value")
                lines += [f"# pad {i}" for i in range(1300, 2501)]
                with open(os.path.join(d, "time.py"), "w") as f:
                    f.write("\n".join(lines))
                ctx = _FakeCtx(d)
                r = await _handle_find_symbol(ctx, {"name": "naturaldate"})
                assert r.ok, r.error
                assert r.data["match_count"] == 1
                m = r.data["matches"][0]
                assert m["file"] == "time.py"
                assert m["kind"] == "function"
                assert m["line_start"] == 1201
                assert "def naturaldate" in m["signature"]

        asyncio.run(_run())

    def test_locates_class(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                with open(os.path.join(d, "m.py"), "w") as f:
                    f.write("class Engine:\n    pass\n\nclass Wheel:\n    pass\n")
                ctx = _FakeCtx(d)
                r = await _handle_find_symbol(ctx, {"name": "Wheel", "kind": "class"})
                assert r.ok
                assert r.data["match_count"] == 1
                assert r.data["matches"][0]["kind"] == "class"

        asyncio.run(_run())

    def test_kind_filter_excludes_other_kinds(self):
        async def _run():
            with tempfile.TemporaryDirectory() as d:
                with open(os.path.join(d, "m.py"), "w") as f:
                    f.write("def x(): pass\nclass x: pass\n")
                ctx = _FakeCtx(d)
                r = await _handle_find_symbol(ctx, {"name": "x", "kind": "function"})
                assert r.ok
                assert r.data["match_count"] == 1
                assert r.data["matches"][0]["kind"] == "function"

        asyncio.run(_run())


# ── helpers: read_file cache key ───────────────────────────────────────────


class TestReadFileCacheKey:
    def test_full_read_key(self):
        assert _read_file_cache_key({"path": "a.py"}) == ("a.py", "full:")

    def test_full_read_with_reason_key_distinct(self):
        assert _read_file_cache_key({"path": "a.py", "reason": "need_full_file"}) == (
            "a.py",
            "full:need_full_file",
        )

    def test_range_read_key(self):
        assert _read_file_cache_key(
            {"path": "a.py", "line_start": 10, "line_end": 50}
        ) == ("a.py", "range:10:50")

    def test_around_line_key(self):
        assert _read_file_cache_key(
            {"path": "a.py", "around_line": 142, "context": 40}
        ) == ("a.py", "around:142:40")

    def test_around_line_different_context_different_key(self):
        k1 = _read_file_cache_key({"path": "a.py", "around_line": 142, "context": 40})
        k2 = _read_file_cache_key({"path": "a.py", "around_line": 142, "context": 80})
        assert k1 != k2


# ── G5/G6: investigation-loop guards ───────────────────────────────────────


def _resp_tool_use(name: str, tool_input: dict, *, use_id: str = "tu1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        text="",
        tool_uses=[LLMToolUse(id=use_id, name=name, input=tool_input)],
    )


def _resp_end_with_fix_spec() -> LLMResponse:
    fix_spec_json = """```json
{
  "root_cause": "test",
  "error_line_quote": "AssertionError: x",
  "affected_files": ["a.py"],
  "fix_spec": "do nothing",
  "failing_command": "pytest",
  "verify_command": "pytest",
  "verify_success": {"exit_codes": [0], "stdout_contains": null, "stderr_excludes": null},
  "confidence": 0.8,
  "open_questions": [],
  "self_critique": {
    "ci_log_addresses_root_cause": true,
    "affected_files_exist_in_repo": true,
    "verify_command_will_distinguish_success": true,
    "notes": "ok"
  },
  "replan_reason": null
}
```"""
    return LLMResponse(stop_reason="end_turn", text=fix_spec_json, tool_uses=[])


class _FakeLLM:
    """Scripted LLM — yields the next response from a queue per turn."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return LLMResponse(stop_reason="end_turn", text="", tool_uses=[])
        return self._responses.pop(0)


class _LoopCtx:
    """Loop-level fake AgentContext (only .messages + .repo_workspace_path used)."""

    def __init__(self, workspace: str) -> None:
        self.repo_workspace_path = workspace
        self.messages: list[dict] = []


class TestBudgetFooter:
    def test_budget_footer_attached_to_every_tool_result(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("def hi(): return 1\n")
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    _resp_tool_use("read_file", {"path": "a.py"}),
                    _resp_end_with_fix_spec(),
                ]
            )

            class _Logger:
                def info(self, *a, **k):
                    pass

                def warning(self, *a, **k):
                    pass

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=5,
                    max_tool_calls=15,
                    logger=_Logger(),
                )

            spec, turns, calls = asyncio.run(_run())
            assert calls == 1
            # The tool_result message is appended after the assistant turn.
            tool_result_msg = next(
                m
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"]
                and m["content"][0].get("type") == "tool_result"
            )
            content = tool_result_msg["content"][0]["content"]
            assert "_tl_budget" in content
            footer = content["_tl_budget"]
            assert footer["tool_calls_used"] == 1
            assert footer["tool_calls_remaining"] == 14
            assert footer["tool_call_cap"] == 15
            assert footer["force_emit_after_turn"] == _FORCE_EMIT_AFTER_TURN


class TestDuplicateReadRejection:
    def test_repeated_read_of_same_range_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("def hi(): return 1\n")
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    _resp_tool_use("read_file", {"path": "a.py"}, use_id="t1"),
                    _resp_tool_use("read_file", {"path": "a.py"}, use_id="t2"),
                    _resp_end_with_fix_spec(),
                ]
            )

            class _Logger:
                def info(self, *a, **k):
                    pass

                def warning(self, *a, **k):
                    pass

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=5,
                    max_tool_calls=15,
                    logger=_Logger(),
                )

            spec, turns, calls = asyncio.run(_run())
            assert calls == 2  # both calls were counted

            tool_results = [
                m["content"][0]
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"]
                and m["content"][0].get("type") == "tool_result"
            ]
            assert len(tool_results) == 2
            # Second one is the rejection
            second = tool_results[1]["content"]
            assert second["ok"] is False
            assert "repeated_read" in second["error"]


class TestForceEmitAtTurn3:
    def test_synthetic_message_injected_at_turn_3(self):
        """Build force-emit message string is well-formed."""
        msg = _build_force_emit_message(tool_calls_used=12, max_tool_calls=15)
        assert "12" in msg and "15" in msg
        assert "ESCALATE" in msg
        assert "FURTHER TOOL CALLS WILL BE REJECTED" in msg

    def test_loop_injects_force_emit_and_blocks_subsequent_tools(self):
        """3 turns of tool_use, then on turn 3 entry the loop injects
        force-emit and rejects the next tool call. Loop still terminates
        because the 4th LLM response is end_turn with fix_spec."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("def hi(): return 1\n")
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    # turn 0: tool call
                    _resp_tool_use("get_pr_context", {}, use_id="t0"),
                    # turn 1: tool call
                    _resp_tool_use("get_pr_context", {}, use_id="t1"),
                    # turn 2: tool call
                    _resp_tool_use("get_pr_context", {}, use_id="t2"),
                    # turn 3: model still tries a tool call — must be rejected
                    _resp_tool_use("get_pr_context", {}, use_id="t3"),
                    # turn 4: model finally emits fix_spec
                    _resp_end_with_fix_spec(),
                ]
            )

            class _Logger:
                events: list[tuple[str, dict]] = []

                def info(self, name, **kw):
                    self.events.append((name, kw))

                def warning(self, *a, **k):
                    pass

            logger = _Logger()

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=8,
                    max_tool_calls=15,
                    logger=logger,
                )

            spec, turns, calls = asyncio.run(_run())
            # force_emit_injected log event should fire exactly once
            inj_events = [e for e in logger.events if e[0] == "cifix_techlead.force_emit_injected"]
            assert len(inj_events) == 1
            assert inj_events[0][1]["turn"] == 3

            # The turn-3 tool call (t3) must come back as forced_emit_or_escalate
            tool_results = [
                m["content"][0]
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"]
                and m["content"][0].get("type") == "tool_result"
            ]
            forced_results = [
                tr for tr in tool_results
                if isinstance(tr.get("content"), dict)
                and "forced_emit_or_escalate" in (tr["content"].get("error") or "")
            ]
            assert len(forced_results) == 1


class TestAppendBudgetFooterHelper:
    def test_helper_writes_budget_block(self):
        out = _append_budget_footer(
            {"ok": True, "data": "x"},
            tool_calls_used=4,
            max_tool_calls=15,
            turn=1,
            max_turns=8,
        )
        assert out["_tl_budget"] == {
            "tool_calls_used": 4,
            "tool_calls_remaining": 11,
            "tool_call_cap": 15,
            "turn": 2,
            "max_turns": 8,
            "force_emit_after_turn": _FORCE_EMIT_AFTER_TURN,
        }
