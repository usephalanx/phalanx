"""Scorecard replay: deterministic regression gate for v2 cells.

Each fixture under `tests/fixtures/scorecard/*/*.json` is a recorded
live run — LLM traffic + tool trace + final outcome. This test
monkeypatches the LLM seams with canned responses and the tool
handlers with canned results, then re-runs `run_ci_fix_v2`. If the
agent loop produces the same decisions + same verdict, the cell
passes.

Purpose
───────
Zero-cost, ~1-second regression gate for every prompt / loop / tool
change. Unit tests catch code bugs; live simulate runs catch
behavior bugs at $0.50/run; replay bridges the gap.

How to add a new fixture
────────────────────────

    # Run a live simulate with --record
    python -m phalanx.ci_fixer_v2.simulate \
      --repo owner/repo --pr N --branch B --sha C --job-id J \
      --failing-command "..." --failing-job-name "..." \
      --record tests/fixtures/scorecard/<lang>/<cell>.json \
      --cell-name <lang>_<cell>

Next pytest run picks it up automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.agent import run_ci_fix_v2
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.replay import (
    Fixture,
    LLMReplayer,
    tool_replay_patcher,
)
from phalanx.ci_fixer_v2.tools import base as tools_base


FIXTURE_ROOT = Path(__file__).parent.parent.parent / "fixtures" / "scorecard"


def _discover_fixtures() -> list[Path]:
    if not FIXTURE_ROOT.exists():
        return []
    return sorted(FIXTURE_ROOT.rglob("*.json"))


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


@pytest.mark.parametrize(
    "fixture_path",
    _discover_fixtures(),
    ids=lambda p: p.parent.name + "/" + p.stem,
)
async def test_scorecard_replay(fixture_path: Path, monkeypatch):
    """Replay one scorecard fixture end-to-end. Asserts the agent loop
    lands the same verdict + escalation reason against the recorded
    inputs."""
    if not fixture_path.exists():
        pytest.skip(f"fixture missing: {fixture_path}")
    fx = Fixture.from_json(fixture_path.read_text())

    # Build fake LLM callables from the recorded traffic. Main and
    # coder are independent replayers keyed by role.
    main_llm = LLMReplayer(role="main", calls=fx.llm_calls)
    coder_llm = LLMReplayer(role="coder", calls=fx.llm_calls)

    # Wire the coder seam so delegate_to_coder picks up the replayer.
    from phalanx.ci_fixer_v2 import coder_subagent as sub_mod

    monkeypatch.setattr(sub_mod, "_call_sonnet_llm", coder_llm)

    # Patch every registered tool's handler to serve canned results in
    # order. This covers main-agent tools AND coder-subagent tools
    # (they share the registry).
    replay_handler, _cursor = tool_replay_patcher(fx.tool_calls)

    for tool_name in list(tools_base._registry):  # type: ignore[attr-defined]
        real_tool = tools_base.get(tool_name)
        # Bind the expected name so tool_replay_patcher can detect drift.
        async def make_handler(expected_name):
            async def _h(ctx, tool_input):
                return await replay_handler(expected_name, ctx, tool_input)
            return _h
        # In-place swap of the handler attribute.
        real_tool.handler = (  # type: ignore[assignment]
            await make_handler(tool_name)
        )

    # Seed a minimal AgentContext from the fixture.
    init = fx.initial_context
    ctx = AgentContext(
        ci_fix_run_id=fx.expected_outcome.get("ci_fix_run_id") or "replay",
        repo_full_name=init.get("repo") or "replay/repo",
        repo_workspace_path=init.get("workspace_path") or "/tmp/replay-ws",
        original_failing_command=init.get("failing_command") or "",
        pr_number=init.get("pr"),
        has_write_permission=True,
        ci_api_key="replay-token",
        sandbox_container_id="replay-sandbox",
        ci_provider="github_actions",
        author_head_branch=init.get("branch"),
    )

    outcome = await run_ci_fix_v2(ctx, main_llm)

    expected = fx.expected_outcome
    assert outcome.verdict.value == expected["verdict"], (
        f"verdict drift in {fixture_path.name}: "
        f"expected {expected['verdict']}, got {outcome.verdict.value}"
    )
    if expected.get("escalation_reason"):
        assert (
            outcome.escalation_reason is not None
            and outcome.escalation_reason.value == expected["escalation_reason"]
        ), (
            f"escalation reason drift in {fixture_path.name}: "
            f"expected {expected['escalation_reason']}, got "
            f"{outcome.escalation_reason.value if outcome.escalation_reason else None}"
        )
