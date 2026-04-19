"""End-to-end integration test against the 3 seed Python fixtures.

Uses scripted LLM responses (no API keys, no docker) to drive the full
v2 agent loop through each fixture's expected resolution path, scores
the result, and asserts the aggregate scoreboard matches expectations.

What this proves:
  1. agent loop + tool registry + tool-scope + provider-neutral message
     format all wire together correctly for at least one happy path per
     failure class.
  2. scoring + scoreboard correctly consumes the (fixture, outcome, ctx)
     triple end-to-end.
  3. seed fixtures are valid (load cleanly + match the expected
     ground-truth decision class).

This is the "proof-first" checkpoint before live LLM wiring in prod.
Live-run instructions + the 80-fixture per-language corpus are in
docs/ci-fixer-v2-live-run.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from phalanx.ci_fixer_v2 import tools as _tools_pkg
from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse, run_ci_fix_v2
from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.simulation.fixtures import Fixture, load_fixture
from phalanx.ci_fixer_v2.simulation.suite import FixtureRunResult, run_suite
from phalanx.ci_fixer_v2.tools import action, base as tools_base, coder


SEED_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "simulation" / "fixtures"


@pytest.fixture(autouse=True)
def _reset_registry_with_builtins():
    tools_base.clear_registry_for_testing()
    _tools_pkg._register_builtin_tools()
    yield
    tools_base.clear_registry_for_testing()


# ─────────────────────────────────────────────────────────────────────────
# Shared mocks — tools that would otherwise shell out to docker / git
# ─────────────────────────────────────────────────────────────────────────


def _install_tool_mocks(monkeypatch, flaky_diff: bool = False):
    """Stub the sandbox + git seams AND the Sonnet LLM seam so the agent
    can 'succeed' without docker / git / API keys.

    - run_in_sandbox → exit 0 ('All checks passed').
    - apply_patch (git apply) → returns clean.
    - commit_and_push (git subprocesses) → each step exits 0 with a
      deterministic sha on rev-parse.
    - delegate_to_coder's final `git diff HEAD` → canned diff.
    - Sonnet subagent LLM → scripted 2-turn sequence: apply_patch then
      run_in_sandbox against the original failing command (which flips
      the verification gate via the mocked exec path).

    When `flaky_diff=True`, the canned final diff includes a
    `@pytest.mark.flaky(...)  # TODO(...):` marker so the scoring
    predictor classifies the decision as `mark_flaky_with_todo`.
    """
    async def fake_docker(_argv, _timeout):
        return (0, "All checks passed!\n", "", False, 0.2)

    monkeypatch.setattr(action, "_exec_argv", fake_docker)
    monkeypatch.setattr(
        action,
        "_build_exec_argv",
        lambda cid, cmd: ["docker", "exec", cid, "sh", "-c", cmd],
    )

    # commit_and_push uses action._run_git_command; stub it with a
    # scripted queue that always returns success and a known sha on
    # rev-parse. Needed so the main loop's final commit step works.
    async def fake_git_command(_ws, args, timeout=60):
        if list(args)[:1] == ["rev-parse"]:
            return (0, "deadbeef0000000000000000000000000000dead\n", "")
        return (0, "", "")

    monkeypatch.setattr(action, "_run_git_command", fake_git_command)

    # apply_patch + delegate_to_coder's final-diff seams live in tools.coder.
    async def fake_git_stdin(_ws, _args, _stdin, timeout=60):
        return (0, "", "")

    monkeypatch.setattr(coder, "_run_git_with_stdin", fake_git_stdin)

    final_diff_text = (
        "diff --git a/tests/t.py b/tests/t.py\n"
        "--- a/tests/t.py\n+++ b/tests/t.py\n"
        "@@ -1 +1,2 @@\n"
        "+@pytest.mark.flaky(reruns=2)  # TODO(PHX-1): upstream SLA\n"
        " def test_x(): pass\n"
        if flaky_diff
        else (
            "diff --git a/stub b/stub\n"
            "--- a/stub\n+++ b/stub\n@@ -1 +1 @@\n-old\n+new\n"
        )
    )

    async def fake_final_diff(_ws):
        return final_diff_text

    monkeypatch.setattr(coder, "_compute_final_diff", fake_final_diff)

    # Coder subagent Sonnet LLM — stateless scripted sequence. Each call
    # inspects the incoming messages to decide which turn we're on:
    #   - 0 assistant messages so far → turn 1 → apply_patch
    #   - 1+ assistant messages        → turn 2 → run_in_sandbox(failing_cmd)
    # This keeps state in the caller's messages list (where it belongs)
    # rather than in the mock, so consecutive delegate_to_coder invocations
    # across different fixtures don't pollute each other.
    import phalanx.ci_fixer_v2.coder_subagent as sub_mod

    async def scripted_sonnet(messages):
        failing_cmd = "ruff check"
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                for line in m["content"].splitlines():
                    if line.startswith("FAILING COMMAND TO VERIFY AGAINST:"):
                        failing_cmd = line.split(":", 1)[1].strip()
                        break

        assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")

        if assistant_turns == 0:
            patch_diff = (
                "diff --git a/stub.py b/stub.py\n"
                "--- a/stub.py\n+++ b/stub.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
            )
            return type(
                "R",
                (),
                {
                    "stop_reason": "tool_use",
                    "text": "applying",
                    "tool_uses": [
                        type(
                            "U",
                            (),
                            {
                                "id": "p1",
                                "name": "apply_patch",
                                "input": {
                                    "diff": patch_diff,
                                    "target_files": ["stub.py"],
                                },
                            },
                        )
                    ],
                    "input_tokens": 40,
                    "output_tokens": 10,
                    "thinking_tokens": 0,
                },
            )()

        # Turn 2+: verify via run_in_sandbox against the actual failing
        # command pulled from this invocation's seed prompt.
        return type(
            "R",
            (),
            {
                "stop_reason": "tool_use",
                "text": "verifying",
                "tool_uses": [
                    type(
                        "U",
                        (),
                        {
                            "id": "v1",
                            "name": "run_in_sandbox",
                            "input": {"command": failing_cmd},
                        },
                    )
                ],
                "input_tokens": 40,
                "output_tokens": 10,
                "thinking_tokens": 0,
            },
        )()

    monkeypatch.setattr(sub_mod, "_call_sonnet_llm", scripted_sonnet)


# ─────────────────────────────────────────────────────────────────────────
# Script builders — one per failure class
# ─────────────────────────────────────────────────────────────────────────


def _script_lint_fix_happy_path(fixture: Fixture) -> list[LLMResponse]:
    """Scripted ideal flow for a lint-class fixture.

    1. delegate_to_coder (applies patch + verifies in sandbox).
    2. comment_on_pr explaining the fix.
    3. commit_and_push.
    """
    first_target_file = "src/api/handlers.py"
    return [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="d1",
                    name="delegate_to_coder",
                    input={
                        "task_description": (
                            "Wrap the string literal across two lines to satisfy "
                            "ruff E501 in " + first_target_file
                        ),
                        "target_files": [first_target_file],
                        "diagnosis_summary": "ruff E501 on the verbose message.",
                        "failing_command": "ruff check src/",
                    },
                )
            ],
            input_tokens=200,
            output_tokens=60,
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="c1",
                    name="comment_on_pr",
                    input={"body": "Wrapped the long string to satisfy E501."},
                )
            ],
            input_tokens=100,
            output_tokens=30,
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="p1",
                    name="commit_and_push",
                    input={
                        "branch_strategy": "author_branch",
                        "commit_message": "style: wrap long string to satisfy ruff E501",
                        "files": [first_target_file],
                    },
                )
            ],
            input_tokens=100,
            output_tokens=30,
        ),
    ]


def _script_flake_mark_with_todo(fixture: Fixture) -> list[LLMResponse]:
    """Scripted ideal flow for a flake: mark flaky with TODO + commit.

    Spec §12 says mark-flaky fixtures pass lenient even without sandbox
    verification when behavioral matches. We still apply a patch via the
    coder subagent so verification flips, keeping the happy-path check
    conservative (sandbox always green).
    """
    test_file = "tests/test_api_integration.py"
    return [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="d1",
                    name="delegate_to_coder",
                    input={
                        "task_description": (
                            "Add @pytest.mark.flaky with a TODO referencing the "
                            "upstream SLA incident."
                        ),
                        "target_files": [test_file],
                        "diagnosis_summary": (
                            "Test timed out once; retry passed. Not this PR's problem."
                        ),
                        "failing_command": "pytest tests/test_api_integration.py -x",
                    },
                )
            ],
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="c1",
                    name="comment_on_pr",
                    input={
                        "body": (
                            "Marking `test_upstream_health_check` as flaky with a "
                            "TODO — upstream SLA is the root cause."
                        )
                    },
                )
            ],
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="p1",
                    name="commit_and_push",
                    input={
                        "branch_strategy": "author_branch",
                        "commit_message": (
                            "test: mark test_upstream_health_check flaky with TODO"
                        ),
                        "files": [test_file],
                    },
                )
            ],
        ),
    ]


def _script_test_fail_fix(fixture: Fixture) -> list[LLMResponse]:
    """Scripted ideal flow for a clear test failure."""
    return [
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="d1",
                    name="delegate_to_coder",
                    input={
                        "task_description": (
                            "Fix the off-by-one in src/pagination.py so the last "
                            "page slice includes the final element."
                        ),
                        "target_files": ["src/pagination.py"],
                        "diagnosis_summary": "slice upper bound is one short.",
                        "failing_command": "pytest tests/test_pagination.py::test_last_page_slice",
                    },
                )
            ],
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="c1",
                    name="comment_on_pr",
                    input={"body": "Fixed pagination off-by-one."},
                )
            ],
        ),
        LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(
                    id="p1",
                    name="commit_and_push",
                    input={
                        "branch_strategy": "author_branch",
                        "commit_message": "fix(pagination): correct slice upper bound",
                        "files": ["src/pagination.py"],
                    },
                )
            ],
        ),
    ]


_SCRIPT_BUILDERS = {
    "lint": _script_lint_fix_happy_path,
    "flake": _script_flake_mark_with_todo,
    "test_fail": _script_test_fail_fix,
}


# ─────────────────────────────────────────────────────────────────────────
# FixtureRunner — scripted LLM + ctx from fixture
# ─────────────────────────────────────────────────────────────────────────


def _build_scripted_runner(tmp_path: Path):
    """Return a FixtureRunner that drives the main loop with the script
    matching each fixture's failure_class.
    """
    async def _runner(fixture: Fixture) -> FixtureRunResult:
        workspace = tmp_path / fixture.fixture_id
        workspace.mkdir(parents=True, exist_ok=True)

        ctx = AgentContext(
            ci_fix_run_id=fixture.fixture_id,
            repo_full_name=fixture.meta.origin_repo or "seed/repo",
            repo_workspace_path=str(workspace),
            original_failing_command="ruff check src/",
            pr_number=fixture.meta.origin_pr_number,
            has_write_permission=True,
            ci_api_key="test-token",
            sandbox_container_id="container-test",
            ci_provider="github_actions",
            fingerprint_hash=fixture.meta.origin_commit_sha[:16] or None,
            author_head_branch="seed-branch",
        )
        # Override the seed command to align with each fixture's class.
        if fixture.meta.failure_class == "test_fail":
            ctx.original_failing_command = (
                "pytest tests/test_pagination.py::test_last_page_slice"
            )
        elif fixture.meta.failure_class == "flake":
            ctx.original_failing_command = "pytest tests/test_api_integration.py -x"

        builder = _SCRIPT_BUILDERS.get(fixture.meta.failure_class)
        if builder is None:
            raise AssertionError(
                f"no script for failure_class={fixture.meta.failure_class}"
            )
        script = builder(fixture)

        script_iter = iter(script)

        async def scripted_llm(_messages):
            return next(script_iter)

        outcome = await run_ci_fix_v2(ctx, scripted_llm)
        return FixtureRunResult(fixture=fixture, outcome=outcome, ctx=ctx)

    return _runner


# ─────────────────────────────────────────────────────────────────────────
# The end-to-end test
# ─────────────────────────────────────────────────────────────────────────


def test_seed_corpus_is_present_and_loads_cleanly():
    # Even before running the loop, verify the 3 seeds are on disk and
    # meet the fixture schema.
    fixtures_dir = SEED_CORPUS_ROOT / "python"
    assert fixtures_dir.exists(), f"expected seed fixtures at {fixtures_dir}"

    loaded: list[str] = []
    for class_dir in sorted(fixtures_dir.iterdir()):
        if not class_dir.is_dir():
            continue
        for fixture_dir in sorted(class_dir.iterdir()):
            fx = load_fixture(fixture_dir)
            loaded.append(fx.fixture_id)

    assert "seed-ruff-e501" in loaded
    assert "seed-pytest-assertion" in loaded
    assert "seed-flaky-network" in loaded


async def test_e2e_suite_runs_all_seed_fixtures_and_scoreboard_renders(tmp_path, monkeypatch):
    # Non-flake classes use the plain stub diff; we assert on suite
    # plumbing here, not on the flake behavioral predictor (which gets
    # its own test below).
    _install_tool_mocks(monkeypatch, flaky_diff=False)

    runner = _build_scripted_runner(tmp_path)
    result = await run_suite(
        corpus_root=SEED_CORPUS_ROOT,
        runner=runner,
        language="python",
    )

    # Every fixture produced a score; no runner errors.
    assert result.fixture_count == 3
    assert result.error_count == 0
    assert len(result.scores) == 3

    # Each score has a valid language + failure_class.
    classes = sorted(s.failure_class for s in result.scores)
    assert classes == ["flake", "lint", "test_fail"]

    # All three scripted runs COMMIT (happy paths).
    verdicts = sorted(s.verdict for s in result.scores)
    assert verdicts == ["committed", "committed", "committed"]

    # Lenient gate holds for all three (sandbox verified via mock).
    assert all(s.lenient for s in result.scores)

    by_class = {s.failure_class: s for s in result.scores}
    assert by_class["lint"].decision_class_predicted == "code_change"
    assert by_class["test_fail"].decision_class_predicted == "code_change"
    # Flake fixture expects mark_flaky_with_todo; with the plain stub
    # diff, the behavioral predictor classifies as code_change (no marker).
    # This is the expected outcome for THIS test — it proves the suite
    # correctly scores a known mismatch.
    assert by_class["flake"].decision_class_expected == "mark_flaky_with_todo"
    assert by_class["flake"].decision_class_predicted == "code_change"
    assert by_class["flake"].behavioral is False

    # Scoreboard renders without errors.
    from phalanx.ci_fixer_v2.simulation.scoreboard import render_markdown

    md = render_markdown(result.scoreboard)
    assert "Simulation Scoreboard" in md
    assert "python" in md


async def test_e2e_flake_fixture_scores_behavioral_pass_with_flaky_marker_diff(
    tmp_path, monkeypatch
):
    """Separate test: when the committed diff includes a flaky marker
    + TODO (simulating an ideal flake fix), the behavioral predictor
    classifies as `mark_flaky_with_todo` and the flake fixture passes
    behavioral."""
    _install_tool_mocks(monkeypatch, flaky_diff=True)

    runner = _build_scripted_runner(tmp_path)
    result = await run_suite(
        corpus_root=SEED_CORPUS_ROOT,
        runner=runner,
        language="python",
        failure_class="flake",
    )
    assert result.fixture_count == 1
    assert len(result.scores) == 1
    flake_score = result.scores[0]
    assert flake_score.decision_class_predicted == "mark_flaky_with_todo"
    assert flake_score.decision_class_expected == "mark_flaky_with_todo"
    assert flake_score.behavioral is True
