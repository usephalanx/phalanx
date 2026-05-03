"""Tier-1 regression tests for the v1.7 Challenger.

Two test sets share the existing v1.7 TL corpus (12 fixtures):

  GOOD-PLAN SET — runs Challenger against the canned good TL outputs.
    Expectation: verdict == "accept" with empty objections.
    Failure mode caught: false rejection (failure mode #6).

  BAD-PLAN SET — programmatically mutates each canned good output to
    inject ONE specific failure mode (e.g. broken verify_command,
    ungrounded step, wrong affected_files). Each mutation maps to an
    objection category in the rubric.
    Expectation: verdict == "block" or "warn" with the right objection
    category.
    Failure mode caught: sycophantic agreement; the rubric is the
    minimum bar — Challenger must catch these classes.

Tier-1 runs WITHOUT any LLM call by default — uses canned mock
verdicts to exercise the harness wiring + parser. Set the env var
`CHALLENGER_REAL_LLM=1` to run real Sonnet against the corpus
(costs ~$2-5 per full run; cached to /tmp/v17_challenger_cache).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from tests.integration.v3_harness.fixtures.v17_tl_corpus.harness import (
    discover_corpus,
)
from tests.integration.v3_harness.test_v17_tl_corpus_harness import _GOOD_OUTPUTS


# ─── Bad-plan mutators — one per rubric category ──────────────────────────────


def _mutate_to_break_verify_command(tl_output: dict) -> dict:
    """R1 trap: replace verify_command with something that won't re-trigger."""
    out = json.loads(json.dumps(tl_output))
    out["verify_command"] = "echo verify_works"
    out["verify_success"] = {"exit_codes": [0]}
    # Mirror in any sre_verify task
    for ts in out.get("task_plan") or []:
        if ts.get("agent") == "cifix_sre_verify":
            for step in ts.get("steps") or []:
                if step.get("action") == "run":
                    step["command"] = "echo verify_works"
                    step["expect_exit"] = 0
    return out


def _mutate_to_loose_verify_success(tl_output: dict) -> dict:
    """R2 trap: verify_success.exit_codes accepts everything."""
    out = json.loads(json.dumps(tl_output))
    out["verify_success"] = {"exit_codes": [0, 1, 2, 3, 4, 5, 6, 7, 8]}
    return out


def _mutate_to_ungrounded_step(tl_output: dict) -> dict:
    """R4 trap: replace the engineer's step `old` text with a hallucination."""
    out = json.loads(json.dumps(tl_output))
    for ts in out.get("task_plan") or []:
        if ts.get("agent") != "cifix_engineer":
            continue
        for step in ts.get("steps") or []:
            if step.get("action") == "replace":
                step["old"] = "this exact text does not exist anywhere in the file"
                break
    return out


def _mutate_to_edit_ci_infrastructure(tl_output: dict) -> dict:
    """R7 trap: engineer step modifies .github/workflows/."""
    out = json.loads(json.dumps(tl_output))
    for ts in out.get("task_plan") or []:
        if ts.get("agent") != "cifix_engineer":
            continue
        steps = ts.get("steps") or []
        steps.insert(0, {
            "id": 0,
            "action": "replace",
            "file": ".github/workflows/test.yml",
            "old": "runs-on: ubuntu-latest",
            "new": "runs-on: ubuntu-22.04",
        })
        ts["steps"] = steps
        break
    return out


def _mutate_to_overconfident_uncalibrated(tl_output: dict) -> dict:
    """R8 trap: confidence 0.99 but verify_command is clearly wrong."""
    out = _mutate_to_break_verify_command(tl_output)
    out["confidence"] = 0.99
    return out


# Map: mutator function → expected Challenger objection category
_BAD_PLAN_MUTATIONS: list[tuple[str, callable, set[str]]] = [
    (
        "broken_verify_command",
        _mutate_to_break_verify_command,
        {"verify_command_does_not_retrigger_failure"},
    ),
    (
        "loose_verify_success",
        _mutate_to_loose_verify_success,
        {"verify_success_too_loose"},
    ),
    (
        "ungrounded_step_old_text",
        _mutate_to_ungrounded_step,
        {"ungrounded_step", "stale_old_text"},  # either is acceptable
    ),
    (
        "edits_ci_infra",
        _mutate_to_edit_ci_infrastructure,
        {"edits_ci_infrastructure"},
    ),
    (
        "overconfident_uncalibrated",
        _mutate_to_overconfident_uncalibrated,
        {"verify_command_does_not_retrigger_failure", "low_confidence_high_stakes"},
    ),
]


# ─── Workspace helpers ────────────────────────────────────────────────────────


def _materialize_workspace(repo_files: dict[str, str], dest: Path) -> None:
    for rel_path, content in repo_files.items():
        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


# ─── Parser-only tests (no LLM, fast) ─────────────────────────────────────────


class TestVerdictParser:
    """Verify _parse_verdict_from_text correctly accepts well-formed
    verdicts and downgrades malformed ones per the hard rules.
    """

    def setup_method(self) -> None:
        from phalanx.agents.cifix_challenger import _parse_verdict_from_text
        self.parse = _parse_verdict_from_text

    def test_accepts_well_formed_accept(self):
        text = """
        ```json
        {"verdict": "accept", "objections": [], "notes": "looks good"}
        ```
        """
        r = self.parse(text)
        assert r is not None
        assert r["verdict"] == "accept"
        assert r["objections"] == []

    def test_accepts_well_formed_block_with_p0(self):
        text = """
        ```json
        {
          "verdict": "block",
          "objections": [{
            "category": "verify_command_does_not_retrigger_failure",
            "severity": "P0",
            "claim": "verify_command is echo, won't fail",
            "evidence": "verify_command: echo verify_works",
            "suggestion": "use the actual pytest invocation"
          }],
          "notes": "verify is broken"
        }
        ```
        """
        r = self.parse(text)
        assert r["verdict"] == "block"
        assert len(r["objections"]) == 1
        assert r["objections"][0]["category"] == "verify_command_does_not_retrigger_failure"

    def test_downgrades_block_without_p0_to_warn(self):
        text = """
        ```json
        {
          "verdict": "block",
          "objections": [{
            "category": "other",
            "severity": "P1",
            "claim": "minor concern",
            "evidence": "stuff"
          }],
          "notes": ""
        }
        ```
        """
        r = self.parse(text)
        # Hard rule: block requires ≥1 P0 — gets downgraded
        assert r["verdict"] == "warn"

    def test_downgrades_warn_without_objections_to_accept(self):
        text = '```json\n{"verdict": "warn", "objections": [], "notes": ""}\n```'
        r = self.parse(text)
        assert r["verdict"] == "accept"

    def test_drops_objection_with_invalid_category(self):
        text = """
        ```json
        {
          "verdict": "block",
          "objections": [
            {"category": "made_up_category", "severity": "P0",
             "claim": "x", "evidence": "y"},
            {"category": "verify_command_does_not_retrigger_failure",
             "severity": "P0", "claim": "real one", "evidence": "real evidence"}
          ],
          "notes": ""
        }
        ```
        """
        r = self.parse(text)
        # Invalid category dropped; valid one kept
        assert r["verdict"] == "block"
        assert len(r["objections"]) == 1
        assert r["objections"][0]["category"] == "verify_command_does_not_retrigger_failure"

    def test_drops_objection_without_evidence(self):
        text = """
        ```json
        {
          "verdict": "block",
          "objections": [
            {"category": "ungrounded_step", "severity": "P0",
             "claim": "no evidence given", "evidence": ""}
          ],
          "notes": ""
        }
        ```
        """
        r = self.parse(text)
        # Cascade: evidence-less objection dropped → block has no P0 → warn
        # → warn has no objections → accept. Sycophantic block becomes a
        # quiet accept, which is what we want.
        assert r["verdict"] == "accept"
        assert r["objections"] == []

    def test_returns_none_on_unparseable(self):
        assert self.parse("no JSON here just prose") is None
        assert self.parse("") is None


# ─── Mutator coverage tests ───────────────────────────────────────────────────


class TestBadPlanMutators:
    """Sanity: each mutator actually changes the output in a way a real
    Challenger could detect."""

    def test_each_mutator_produces_distinct_output(self):
        corpus = {f.name: f for f in discover_corpus()}
        # Use a stable canned output that has all the surfaces we mutate
        base = _GOOD_OUTPUTS["02_importerror_missing_dep"]()
        seen = set()
        for name, mutator, _expected_cats in _BAD_PLAN_MUTATIONS:
            mutated = mutator(base)
            blob = json.dumps(mutated, sort_keys=True)
            assert blob != json.dumps(base, sort_keys=True), (
                f"mutator {name!r} did not change the output"
            )
            assert blob not in seen, f"mutator {name!r} output collides with another"
            seen.add(blob)


# ─── Real-LLM regression suite (gated by env) ────────────────────────────────


_REAL_LLM = os.environ.get("CHALLENGER_REAL_LLM") == "1"


@pytest.mark.skipif(not _REAL_LLM, reason="set CHALLENGER_REAL_LLM=1 to run")
class TestChallengerAgainstGoodPlans:
    """For each corpus fixture, run real Challenger on the canned good
    output. Expect ACCEPT (with possibly some non-blocking warns)."""

    @pytest.mark.parametrize(
        "fixture_name",
        sorted(_GOOD_OUTPUTS.keys()),
    )
    def test_good_plan_not_blocked(self, fixture_name: str):
        from phalanx.agents.cifix_challenger import run_challenger_against

        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus[fixture_name]
        tl_output = _GOOD_OUTPUTS[fixture_name]()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _materialize_workspace(fixture.repo_files, workspace)
            verdict = asyncio.run(
                run_challenger_against(
                    tl_output=tl_output,
                    workspace_path=str(workspace),
                    ci_log_text=fixture.ci_log_text,
                    run_id=f"good-{fixture_name}",
                    cache_dir="/tmp/v17_challenger_cache",
                )
            )

        assert verdict["verdict"] != "block", (
            f"Challenger blocked a good plan for {fixture_name}: "
            f"{json.dumps(verdict, indent=2)}"
        )


@pytest.mark.skipif(not _REAL_LLM, reason="set CHALLENGER_REAL_LLM=1 to run")
class TestChallengerCatchesAdversarialMutations:
    """For each fixture × mutation, run real Challenger; expect block or warn
    with at least one objection in the expected category set.
    """

    @pytest.mark.parametrize(
        "fixture_name,mutation_name,mutator,expected_categories",
        [
            (fx, mname, mfn, mcats)
            for fx in sorted(_GOOD_OUTPUTS.keys())
            for (mname, mfn, mcats) in _BAD_PLAN_MUTATIONS
        ],
    )
    def test_challenger_catches_mutation(
        self,
        fixture_name: str,
        mutation_name: str,
        mutator,
        expected_categories: set[str],
    ):
        from phalanx.agents.cifix_challenger import run_challenger_against

        corpus = {f.name: f for f in discover_corpus()}
        fixture = corpus[fixture_name]
        tl_output = _GOOD_OUTPUTS[fixture_name]()
        bad_tl_output = mutator(tl_output)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _materialize_workspace(fixture.repo_files, workspace)
            verdict = asyncio.run(
                run_challenger_against(
                    tl_output=bad_tl_output,
                    workspace_path=str(workspace),
                    ci_log_text=fixture.ci_log_text,
                    run_id=f"bad-{fixture_name}-{mutation_name}",
                    cache_dir="/tmp/v17_challenger_cache",
                )
            )

        assert verdict["verdict"] in {"block", "warn"}, (
            f"Challenger accepted adversarial mutation {mutation_name!r} "
            f"on {fixture_name}: {json.dumps(verdict, indent=2)}"
        )
        seen_cats = {o["category"] for o in verdict.get("objections", [])}
        assert seen_cats & expected_categories, (
            f"Challenger raised objections {seen_cats} but expected at least "
            f"one of {expected_categories} for mutation {mutation_name!r}: "
            f"{json.dumps(verdict, indent=2)}"
        )
