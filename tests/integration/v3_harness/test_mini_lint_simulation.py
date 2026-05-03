"""Mini lint coordination simulation — adversarial seam tests for v1.7.

PURPOSE
=======
Tier-1 tests confirm each agent works in isolation. This file confirms
the AGENTS COORDINATE — what one writes is correctly read by the next.
The two prod incidents this month were both seam bugs (Challenger queue
subscription drop, SRE verify scope ignoring TL.verify_command). Both:
each piece tested green; the contract between them silently drifted.

DESIGN PRINCIPLES
=================
1. Don't synthesize fixtures to make agents pass. Use shapes from the
   agents' OWN documented contracts (cifix_techlead.py:314 etc.) as the
   floor — and adversarial variants on top to challenge them.
2. Each variant simulates a real failure mode we've seen or could see in
   prod: malformed task_plan, missing verify_command, multiple engineer
   tasks, broad-vs-narrow verify_command mismatches, etc.
3. The harness reports a ledger: for each variant, did the chain of
   handoffs hold? Where did the baton drop?

REPLAY PATH
===========
`tests/integration/v3_harness/fixtures/real_runs/*.json` holds captured
prod task rows. `test_replay_real_run` walks each, replays the chain,
and surfaces real seam failures from real data. See that dir's README
for how to capture.

If a test fails, the message tells you exactly which baton was dropped
and what context was lost. Failure noise is the point — synthetic green
is what bit us.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.cifix_engineer import _extract_v17_engineer_steps
from phalanx.agents.cifix_sre import CIFixSREAgent
from phalanx.ci_fixer_v3.provisioner import ExecResult


# ─────────────────────────────────────────────────────────────────────────────
# In-memory simulation primitives
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SimulatedDB:
    """In-memory ledger of completed tasks for a single run.

    Replicates the surface that downstream agents query Postgres for:
    `select Task.output where run_id=... and agent_role=... and status='COMPLETED'`.
    """
    run_id: str = "run-mini-sim"
    rows: list[dict] = field(default_factory=list)
    handoff_log: list[str] = field(default_factory=list)

    def write(self, *, sequence_num: int, agent_role: str, output: dict) -> None:
        self.rows.append({
            "sequence_num": sequence_num,
            "agent_role": agent_role,
            "status": "COMPLETED",
            "output": output,
        })
        self.handoff_log.append(
            f"WROTE   seq={sequence_num} role={agent_role} keys={sorted(output.keys())}"
        )

    def latest(self, agent_roles: list[str]) -> dict | None:
        for r in sorted(self.rows, key=lambda x: -x["sequence_num"]):
            if r["agent_role"] in agent_roles and r["status"] == "COMPLETED":
                return r["output"]
        return None

    def first_setup(self) -> dict | None:
        sre_roles = {"cifix_sre", "cifix_sre_setup", "cifix_sre_verify"}
        for r in sorted(self.rows, key=lambda x: x["sequence_num"]):
            if (
                r["agent_role"] in sre_roles
                and r["status"] == "COMPLETED"
                and isinstance(r["output"], dict)
                and r["output"].get("mode") == "setup"
            ):
                return r["output"]
        return None


def _fake_session_cm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def _run_inspector(
    db: SimulatedDB, *, exec_exit_code: int = 0
) -> tuple[Any, list[dict]]:
    """Drive `_execute_verify` end-to-end against the SimulatedDB.

    Returns (AgentResult, captured exec calls). All upstream loaders are
    patched to read from `db`. _exec_in_container is patched to record
    every command run + return the stub exit code.
    """
    agent = CIFixSREAgent(
        run_id=db.run_id, agent_id="cifix_sre_verify", task_id="task-verify"
    )
    captured: list[dict] = []

    async def _capture(*args, **kwargs):
        captured.append({"cmd": kwargs.get("cmd", ""), "kwargs": kwargs})
        return ExecResult(ok=(exec_exit_code == 0), exit_code=exec_exit_code)

    with (
        patch.object(
            agent, "_load_upstream_sre_setup",
            AsyncMock(return_value=db.first_setup()),
        ),
        patch.object(
            agent, "_load_upstream_tl_fix_spec",
            AsyncMock(return_value=db.latest(["cifix_techlead"])),
        ),
        patch("phalanx.agents.cifix_sre._exec_in_container", side_effect=_capture),
        patch("phalanx.agents.cifix_sre.get_db", return_value=_fake_session_cm()),
    ):
        result = await agent._execute_verify(
            ci_context={"failing_command": "ruff check ."}
        )
    return result, captured


# ─────────────────────────────────────────────────────────────────────────────
# Adversarial scenarios (shape matches cifix_techlead.py:314 emit spec)
# ─────────────────────────────────────────────────────────────────────────────
# These are NOT synthesized to make the agents pass — they're the failure
# shapes we've seen or could plausibly see in prod, mapped onto the
# documented contract.


def _baseline_sre_setup() -> dict:
    """Canonical SRE setup output — TL/Engineer/Inspector all read this."""
    return {
        "mode": "setup",
        "container_id": "sandbox_abc123",
        "workspace_path": "/tmp/v3-mini/workspace",
        "env_spec": {"stack": "python", "base_image": "python:3.12-slim"},
        "final_status": "READY",
        "tier_used": "0",
    }


def _wellformed_lint_tl_output() -> dict:
    """TL output matching the spec at cifix_techlead.py:314.

    A real-shape lint fix: replace + commit + push, with a NARROW
    verify_command targeting the single affected file.
    """
    return {
        "root_cause": "src/calc/formatting.py:5 line exceeds 100 chars (E501)",
        "affected_files": ["src/calc/formatting.py"],
        "fix_spec": "wrap the long return string across two lines",
        "failing_command": "ruff check src/calc/formatting.py",
        "verify_command": "ruff check src/calc/formatting.py",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.92,
        "error_line_quote": "src/calc/formatting.py:5:101: E501 line too long (104 > 100)",
        "task_plan": [
            {
                "task_id": "T2",
                "agent": "cifix_engineer",
                "depends_on": [],
                "purpose": "wrap long line",
                "steps": [
                    {
                        "id": 1,
                        "action": "replace",
                        "file": "src/calc/formatting.py",
                        "old": '    return "a really long line"',
                        "new": '    return (\n        "a really "\n        "long line"\n    )',
                    },
                    {"id": 2, "action": "commit", "message": "fix(lint): wrap long line"},
                    {"id": 3, "action": "push"},
                ],
            },
            {
                "task_id": "T3",
                "agent": "cifix_sre_verify",
                "depends_on": ["T2"],
                "purpose": "verify",
                "steps": [
                    {"id": 1, "action": "run",
                     "command": "ruff check src/calc/formatting.py",
                     "expect_exit": 0}
                ],
            },
        ],
        "self_critique": {
            "ci_log_addresses_root_cause": True,
            "affected_files_exist_in_repo": True,
            "verify_command_will_distinguish_success": True,
            "grounding_satisfied": True,
            "step_preconditions_satisfied": True,
            "error_line_quoted_from_log": True,
            "notes": "ok",
        },
        "model": "gpt-5.4",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Baseline — well-formed real-shape TL output, full chain holds
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_S1_baseline_full_chain_holds():
    """Variant: well-formed TL output matches the documented spec.
    Every downstream loader must successfully consume it. Inspector
    must run TL's NARROW verify_command, not the broad ci_context one.
    """
    db = SimulatedDB()
    db.write(sequence_num=1, agent_role="cifix_sre_setup", output=_baseline_sre_setup())
    db.write(sequence_num=2, agent_role="cifix_techlead", output=_wellformed_lint_tl_output())
    db.write(sequence_num=3, agent_role="cifix_challenger",
             output={"verdict": "accept", "objections": [], "shadow_mode": True})
    db.write(sequence_num=4, agent_role="cifix_engineer",
             output={"commit_sha": "abc1234", "v17_step_interpreter": True})

    # Engineer's extractor must find the steps
    tl_out = db.latest(["cifix_techlead"])
    steps = _extract_v17_engineer_steps(tl_out)
    assert steps is not None and len(steps) == 3, (
        f"BATON DROP: Engineer extractor returned {steps!r} for well-formed task_plan; "
        f"shape may have drifted from cifix_techlead.py:314 spec"
    )
    assert steps[0]["action"] == "replace"
    assert steps[0]["file"] == "src/calc/formatting.py"

    # Inspector must run TL's narrow verify_command (NOT ci_context broad one)
    result, exec_calls = await _run_inspector(db, exec_exit_code=0)
    assert len(exec_calls) == 1, (
        f"BATON DROP: Inspector ran {len(exec_calls)} commands; expected 1 (narrow). "
        "If >1, fan-out to broad workflow enumeration regressed."
    )
    assert exec_calls[0]["cmd"] == "ruff check src/calc/formatting.py", (
        f"BATON DROP: Inspector ran {exec_calls[0]['cmd']!r}; expected TL's narrow "
        f"verify_command. ci_context.failing_command was 'ruff check .' — that's the "
        f"broad version we must NOT run."
    )
    assert result.output["verdict"] == "all_green"
    assert result.output["verify_scope"] == "narrow_from_tl"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Adversarial — TL emits NO verify_command (low-confidence path)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_S2_tl_omits_verify_command_inspector_falls_back(tmp_path):
    """Real failure mode: TL's confidence is low or it abstains. fix_spec
    has root_cause but no verify_command. Inspector MUST gracefully fall
    back to broad enumeration — not crash, not loop.
    """
    db = SimulatedDB()
    setup = _baseline_sre_setup()
    setup["workspace_path"] = str(tmp_path)  # empty workspace = no workflow YAML
    db.write(sequence_num=1, agent_role="cifix_sre_setup", output=setup)
    db.write(sequence_num=2, agent_role="cifix_techlead", output={
        "root_cause": "uncertain — multi-file failure",
        "fix_spec": "...",
        "confidence": 0.3,
        # NO verify_command, NO verify_success
    })

    result, exec_calls = await _run_inspector(db, exec_exit_code=0)
    # Empty workspace → only original failing_command runs through legacy path
    assert "verify_scope" not in result.output, (
        "Inspector incorrectly took narrow path despite missing verify_command"
    )
    assert len(exec_calls) >= 1
    assert exec_calls[0]["cmd"] == "ruff check ."  # ci_context fallback


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Adversarial — TL emits empty/whitespace verify_command
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_S3_tl_emits_whitespace_verify_command(tmp_path):
    """Trickier adversarial: TL technically wrote verify_command but it's
    whitespace. Naive `if verify_command:` check would route to narrow
    path and run an empty cmd. Must treat as missing and fall back.
    """
    db = SimulatedDB()
    setup = _baseline_sre_setup()
    setup["workspace_path"] = str(tmp_path)
    db.write(sequence_num=1, agent_role="cifix_sre_setup", output=setup)
    db.write(sequence_num=2, agent_role="cifix_techlead", output={
        "root_cause": "...",
        "verify_command": "   \t  ",  # whitespace only
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.5,
    })

    result, exec_calls = await _run_inspector(db, exec_exit_code=0)
    assert "verify_scope" not in result.output, (
        "BATON DROP: Inspector took narrow path with whitespace verify_command. "
        "It would have run an empty shell command in the container."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Adversarial — multiple engineer tasks in plan
# ─────────────────────────────────────────────────────────────────────────────


def test_S4_multiple_engineer_tasks_extractor_picks_first():
    """When TL splits a fix across two engineer tasks (e.g. add dep + edit
    code), the step interpreter should still find a steps list. Today's
    extractor returns the FIRST engineer task's steps. Pin that behavior
    so refactors don't silently change which steps get run.
    """
    tl_out = {
        "task_plan": [
            {"task_id": "T2", "agent": "cifix_sre_setup", "steps": []},
            {"task_id": "T3", "agent": "cifix_engineer",
             "steps": [{"id": 1, "action": "replace", "file": "a.py", "old": "x", "new": "y"}]},
            {"task_id": "T4", "agent": "cifix_engineer",
             "steps": [{"id": 1, "action": "replace", "file": "b.py", "old": "x", "new": "y"}]},
        ]
    }
    steps = _extract_v17_engineer_steps(tl_out)
    assert steps is not None
    # First engineer task wins. If that ever changes, it's a contract change.
    assert steps[0]["file"] == "a.py", (
        f"BATON DROP: extractor picked steps modifying {steps[0].get('file')!r}; "
        "spec is FIRST engineer task. Multi-task plans are now ambiguous."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Adversarial — task_plan in WRONG shape (dict instead of list)
# ─────────────────────────────────────────────────────────────────────────────


def test_S5_task_plan_wrong_shape_extractor_returns_none():
    """If TL emits task_plan as a dict (legacy v1.6.x shape, or LLM hallucination),
    extractor must return None so engineer falls back to v1.6 LLM coder path.
    Surface the contract clearly: ONLY list-shaped task_plans are accepted.
    """
    # Common malformed shapes the LLM might produce
    for malformed in [
        {"task_plan": {"tasks": [{"agent": "cifix_engineer", "steps": [{"action": "replace"}]}]}},
        {"task_plan": "not a list at all"},
        {"task_plan": None},
        {},  # missing entirely
    ]:
        steps = _extract_v17_engineer_steps(malformed)
        assert steps is None, (
            f"BATON DROP: extractor accepted malformed task_plan {malformed!r} "
            f"and returned {steps!r}. Engineer would invoke v1.7 step interpreter "
            "with garbage input."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Adversarial — verify_success matcher edge cases
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_S6_verify_success_matcher_honors_nonzero_exit_codes():
    """Some valid commands return non-zero on success (e.g. coverage with
    --fail-under, or `grep` returning 1 when no match found IS the
    success state). TL can declare exit_codes: [0, 1]. Inspector MUST
    honor that, not collapse everything to exit==0.
    """
    db = SimulatedDB()
    db.write(sequence_num=1, agent_role="cifix_sre_setup", output=_baseline_sre_setup())
    db.write(sequence_num=2, agent_role="cifix_techlead", output={
        "verify_command": "grep -q 'old_string' README.md",
        "verify_success": {"exit_codes": [1]},  # 1 = "no match" = success
        "confidence": 0.9,
    })

    result, exec_calls = await _run_inspector(db, exec_exit_code=1)
    assert result.output["verdict"] == "all_green", (
        f"BATON DROP: matcher rejected exit_code=1 even though TL declared it valid. "
        f"This breaks the 'grep returns 1 = success' contract."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: Adversarial — broad-vs-narrow scope mismatch (THE BUG)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_S7_inspector_ignores_broad_ci_context_when_TL_narrow():
    """The exact prod failure mode from run f5ffd0c8 (lint cell):
      - ci_context.failing_command = 'ruff check .'        (broad — workflow YAML)
      - TL.verify_command          = 'ruff check src/foo.py' (narrow — TL's choice)

    Pre-v1.7.2.2: Inspector ran broad → found unrelated lint elsewhere
    → falsely reported new_failures → DAG looped to turn cap.

    Post-v1.7.2.2: Inspector must run NARROW.
    """
    db = SimulatedDB()
    db.write(sequence_num=1, agent_role="cifix_sre_setup", output=_baseline_sre_setup())
    db.write(sequence_num=2, agent_role="cifix_techlead", output={
        "verify_command": "ruff check src/calc/formatting.py",  # NARROW
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.92,
    })

    # Inspector receives BROAD ci_context (mimicking what commander passes)
    result, exec_calls = await _run_inspector(db, exec_exit_code=0)
    assert len(exec_calls) == 1, (
        f"BATON DROP (the v1.7.2.2 bug): Inspector ran {len(exec_calls)} commands "
        "instead of 1. Broad enumeration regressed."
    )
    assert exec_calls[0]["cmd"] == "ruff check src/calc/formatting.py", (
        f"BATON DROP: Inspector ran {exec_calls[0]['cmd']!r} — that's the broad "
        f"ci_context command, not TL's narrow verify_command. The v1.7.2.2 fix "
        "was supposed to flip this priority."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Replay — load real prod run dumps if any exist
# ─────────────────────────────────────────────────────────────────────────────


_REAL_RUN_DIR = (
    Path(__file__).parent / "fixtures" / "real_runs"
)


def _real_run_files() -> list[Path]:
    if not _REAL_RUN_DIR.is_dir():
        return []
    return sorted(p for p in _REAL_RUN_DIR.glob("*.json"))


@pytest.mark.parametrize(
    "fixture_path", _real_run_files(),
    ids=lambda p: p.stem if hasattr(p, "stem") else str(p),
)
@pytest.mark.asyncio
async def test_replay_real_run(fixture_path: Path):
    """Replay a captured prod run through the agent chain. The fixture is
    a JSON list of completed task rows; we walk them in sequence_num
    order and assert each downstream loader could consume the upstream's
    output.

    This is the strongest seam test we have — it's literally what prod
    produced. Failures here are real bugs in the contract layer.

    To add a fixture, see fixtures/real_runs/README.md.
    """
    rows = json.loads(fixture_path.read_text())
    assert isinstance(rows, list), f"{fixture_path}: expected JSON list of task rows"

    db = SimulatedDB(run_id=f"replay-{fixture_path.stem}")
    for row in sorted(rows, key=lambda r: r["sequence_num"]):
        if row.get("status") != "COMPLETED" or row.get("output") is None:
            db.handoff_log.append(
                f"SKIPPED seq={row['sequence_num']} role={row['agent_role']} "
                f"status={row.get('status')}"
            )
            continue
        db.write(
            sequence_num=row["sequence_num"],
            agent_role=row["agent_role"],
            output=row["output"],
        )

    # Contract checks (only run for the agents present in this dump):
    sre_setup = db.first_setup()
    tl = db.latest(["cifix_techlead"])

    if sre_setup is not None:
        assert "container_id" in sre_setup or "workspace_path" in sre_setup, (
            f"REPLAY {fixture_path.stem}: SRE setup lacks both container_id "
            "and workspace_path — downstream agents would have no sandbox"
        )

    if tl is not None:
        # Plan-validator-style structural check
        plan = tl.get("task_plan")
        if plan is not None and not isinstance(plan, list):
            pytest.fail(
                f"REPLAY {fixture_path.stem}: TL.task_plan is {type(plan).__name__}, "
                "spec requires list (cifix_techlead.py:314)"
            )

        # If TL provided verify_command, it must be a non-empty string
        vc = tl.get("verify_command")
        if vc is not None:
            assert isinstance(vc, str) and vc.strip(), (
                f"REPLAY {fixture_path.stem}: TL emitted verify_command={vc!r} "
                "(empty/non-string). Inspector would route to narrow path with "
                "an unrunnable command."
            )

    # If we have BOTH setup and TL, replay Inspector and assert it ran
    # the right command (narrow if TL.verify_command, broad fallback otherwise).
    if sre_setup is not None and tl is not None:
        result, exec_calls = await _run_inspector(db, exec_exit_code=0)
        if tl.get("verify_command", "").strip():
            assert result.output.get("verify_scope") == "narrow_from_tl", (
                f"REPLAY {fixture_path.stem}: TL had verify_command but Inspector "
                "fell back to broad. v1.7.2.2 bug regressed."
            )
            assert len(exec_calls) == 1
            assert exec_calls[0]["cmd"] == tl["verify_command"].strip()
