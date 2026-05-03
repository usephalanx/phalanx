"""Tier-1 tests for v1.7.2.4 GitHub check-runs re-confirm gate.

The gate is the last line of defense before Commander finalizes SHIP.
SRE Verify's narrow command may pass while GitHub's full CI is still
red (TL targeted the wrong job, engineer's edit broke an unrelated
check). These tests pin the decision logic.

Five scenarios per the v1.7.2.4 spec:
  1. true green
  2. narrow green but a previously-failing GitHub check is still failing
  3. previously-green check regressed to failure
  4. pending checks past poll timeout
  5. missing check-run data (empty response from GitHub)
"""

from __future__ import annotations

from phalanx.agents._github_check_gate import (
    CheckGateVerdict,
    CheckSummary,
    decide,
)


def _check(
    name: str, conclusion: str | None, status: str = "completed",
    html_url: str = "https://github.com/x/y/runs/1",
    summary: str | None = None,
) -> CheckSummary:
    return CheckSummary(
        name=name, conclusion=conclusion, status=status,
        html_url=html_url, summary=summary,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. TRUE_GREEN — every previously-failing check is now success
# ─────────────────────────────────────────────────────────────────────────────


class TestTrueGreen:
    def test_single_check_failure_to_success(self):
        verdict = decide(
            base_checks={"Lint": _check("Lint", "failure")},
            head_checks={"Lint": _check("Lint", "success")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "TRUE_GREEN"
        assert verdict.fixed == ["Lint"]
        assert verdict.regressed == []
        assert verdict.still_failing == []

    def test_multi_check_all_recovered(self):
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "failure"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
                "Build": _check("Build", "success"),
            },
            head_checks={
                "Lint": _check("Lint", "success"),
                "Test + Coverage": _check("Test + Coverage", "success"),
                "Build": _check("Build", "success"),
            },
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "TRUE_GREEN"
        assert sorted(verdict.fixed) == ["Lint", "Test + Coverage"]

    def test_neutral_conclusion_treated_as_pass(self):
        verdict = decide(
            base_checks={"X": _check("X", "failure")},
            head_checks={"X": _check("X", "neutral")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "TRUE_GREEN"

    def test_skipped_conclusion_treated_as_pass(self):
        """Skipped jobs (e.g. matrix excludes) shouldn't trigger NOT_FIXED."""
        verdict = decide(
            base_checks={"X": _check("X", "skipped")},
            head_checks={"X": _check("X", "skipped")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "TRUE_GREEN"

    def test_cifix_own_check_ignored(self):
        """The bot's own progress check (cifix/...) must never gate the run."""
        verdict = decide(
            base_checks={"Lint": _check("Lint", "failure")},
            head_checks={
                "Lint": _check("Lint", "success"),
                "cifix/v3-progress": _check("cifix/v3-progress", "failure"),
            },
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "TRUE_GREEN"
        assert "cifix/v3-progress" not in verdict.post_checks


# ─────────────────────────────────────────────────────────────────────────────
# 2. NOT_FIXED — narrow verify said green, but GitHub still red
# ─────────────────────────────────────────────────────────────────────────────


class TestNotFixed:
    def test_originally_failing_still_failing(self):
        """The coverage cell shape: narrow ruff-format passed, but the
        original failing Test+Coverage job is still red."""
        verdict = decide(
            base_checks={"Test + Coverage": _check("Test + Coverage", "failure")},
            head_checks={"Test + Coverage": _check("Test + Coverage", "failure")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "NOT_FIXED"
        assert verdict.still_failing == ["Test + Coverage"]
        assert verdict.regressed == []

    def test_some_fixed_but_others_still_failing(self):
        """Mixed: TL fixed one job, but another is still failing.
        NOT_FIXED takes precedence — we don't ship a partial fix."""
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "failure"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
            },
            head_checks={
                "Lint": _check("Lint", "success"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
            },
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "NOT_FIXED"
        assert verdict.fixed == ["Lint"]
        assert verdict.still_failing == ["Test + Coverage"]

    def test_cancelled_treated_as_failure(self):
        verdict = decide(
            base_checks={"X": _check("X", "failure")},
            head_checks={"X": _check("X", "cancelled")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "NOT_FIXED"

    def test_timed_out_treated_as_failure(self):
        verdict = decide(
            base_checks={"X": _check("X", "failure")},
            head_checks={"X": _check("X", "timed_out")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "NOT_FIXED"


# ─────────────────────────────────────────────────────────────────────────────
# 3. REGRESSION — previously-green check broke
# ─────────────────────────────────────────────────────────────────────────────


class TestRegression:
    def test_previously_green_now_failure(self):
        """The flake-cell shape: TL fixed Test+Coverage, but engineer's
        edit broke Lint (which was green before)."""
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "success"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
            },
            head_checks={
                "Lint": _check("Lint", "failure"),
                "Test + Coverage": _check("Test + Coverage", "success"),
            },
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "REGRESSION"
        assert verdict.regressed == ["Lint"]
        assert verdict.fixed == ["Test + Coverage"]

    def test_regression_takes_priority_over_not_fixed(self):
        """If both regression AND not-fixed are present, regression wins
        (it's the worse signal — we ALSO failed to fix AND broke something)."""
        verdict = decide(
            base_checks={
                "A": _check("A", "success"),  # was green
                "B": _check("B", "failure"),  # was red
            },
            head_checks={
                "A": _check("A", "failure"),  # broke
                "B": _check("B", "failure"),  # still red
            },
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "REGRESSION"
        assert verdict.regressed == ["A"]
        assert verdict.still_failing == ["B"]

    def test_neutral_to_failure_is_regression(self):
        verdict = decide(
            base_checks={"X": _check("X", "neutral")},
            head_checks={"X": _check("X", "failure")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "REGRESSION"


# ─────────────────────────────────────────────────────────────────────────────
# 4. PENDING_TIMEOUT — checks didn't settle
# ─────────────────────────────────────────────────────────────────────────────


class TestPendingTimeout:
    def test_pending_check_blocks_ship(self):
        """A check still in_progress means we don't yet know the outcome.
        Conservative default: don't ship."""
        verdict = decide(
            base_checks={"X": _check("X", "failure")},
            head_checks={"X": _check("X", None, status="in_progress")},
            base_sha="abc", head_sha="def",
            poll_seconds=300,
        )
        assert verdict.decision == "PENDING_TIMEOUT"
        assert "X" in verdict.pending

    def test_queued_check_counts_as_pending(self):
        verdict = decide(
            base_checks={"X": _check("X", "failure")},
            head_checks={"X": _check("X", None, status="queued")},
            base_sha="abc", head_sha="def",
            poll_seconds=300,
        )
        assert verdict.decision == "PENDING_TIMEOUT"

    def test_one_pending_blocks_even_if_others_passed(self):
        """A single pending check is enough to block — we can't say the
        run is fully green until everything settles."""
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "failure"),
                "Test": _check("Test", "failure"),
            },
            head_checks={
                "Lint": _check("Lint", "success"),
                "Test": _check("Test", None, status="in_progress"),
            },
            base_sha="abc", head_sha="def",
            poll_seconds=300,
        )
        assert verdict.decision == "PENDING_TIMEOUT"
        assert verdict.pending == ["Test"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. MISSING_DATA — GitHub returned no check-runs at all
# ─────────────────────────────────────────────────────────────────────────────


class TestMissingData:
    def test_empty_head_checks_returns_missing_data(self):
        """Workflows haven't been scheduled yet, or the API failed.
        Conservative: don't ship."""
        verdict = decide(
            base_checks={"Lint": _check("Lint", "failure")},
            head_checks={},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "MISSING_DATA"
        assert "no check-runs returned" in (verdict.notes or "").lower()

    def test_empty_base_checks_does_not_force_missing_data(self):
        """If base has no checks but head does and they're all green,
        we still ship — there's nothing to compare to but nothing's
        broken either. 'Missing' refers to head, not base."""
        verdict = decide(
            base_checks={},
            head_checks={"Lint": _check("Lint", "success")},
            base_sha="abc", head_sha="def",
        )
        assert verdict.decision == "TRUE_GREEN"


# ─────────────────────────────────────────────────────────────────────────────
# Ignore lists
# ─────────────────────────────────────────────────────────────────────────────


class TestIgnoreList:
    def test_explicit_ignore_drops_from_decision(self):
        """Caller can pass ignore_check_names to skip checks Phalanx
        already verified out-of-band (e.g. failing_job_id)."""
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "failure"),
                "Skip Me": _check("Skip Me", "failure"),
            },
            head_checks={
                "Lint": _check("Lint", "success"),
                "Skip Me": _check("Skip Me", "failure"),  # would block, but ignored
            },
            base_sha="abc", head_sha="def",
            ignore_check_names=frozenset({"Skip Me"}),
        )
        assert verdict.decision == "TRUE_GREEN"
        assert "Skip Me" not in verdict.post_checks


# ─────────────────────────────────────────────────────────────────────────────
# Verdict serialization (for AgentResult.output + escalation_record)
# ─────────────────────────────────────────────────────────────────────────────


class TestVerdictSerialization:
    def test_to_dict_includes_summary_and_urls(self):
        verdict = decide(
            base_checks={"Lint": _check("Lint", "failure",
                                        html_url="https://gh/lint/1")},
            head_checks={"Lint": _check("Lint", "failure",
                                        html_url="https://gh/lint/2",
                                        summary="E501 line too long")},
            base_sha="abc", head_sha="def",
        )
        d = verdict.to_dict()
        assert d["decision"] == "NOT_FIXED"
        assert d["still_failing"] == ["Lint"]
        assert d["post_checks"]["Lint"]["html_url"] == "https://gh/lint/2"
        assert d["post_checks"]["Lint"]["summary"] == "E501 line too long"

    def test_to_dict_serializable(self):
        """Output must be JSON-serializable (no dataclass leakage) for
        Postgres JSONB storage in runs.error_context."""
        import json
        verdict = decide(
            base_checks={"X": _check("X", "failure")},
            head_checks={"X": _check("X", "success")},
            base_sha="abc", head_sha="def",
        )
        s = json.dumps(verdict.to_dict())
        # Round-trip back
        d = json.loads(s)
        assert d["decision"] == "TRUE_GREEN"


# ─────────────────────────────────────────────────────────────────────────────
# Coverage-cell + flake-cell shape replays (from the 2026-05-03 phase-2 report)
# ─────────────────────────────────────────────────────────────────────────────


class TestPhase2RegressionReplays:
    """Replays the EXACT shapes that today's gateless v1.7.2.3 run shipped
    incorrectly. With the gate, both must NOT ship.
    """

    def test_coverage_cell_replay_blocks(self):
        """Coverage cell ran d9005ec6: TL chose 'ruff format --check src/calc/math_ops.py'
        as verify_command but the actual failing job 'Test + Coverage'
        was still red. Phalanx shipped it; GitHub said failure.
        With the gate: NOT_FIXED → no ship."""
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "failure"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
            },
            head_checks={
                "Lint": _check("Lint", "success"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
            },
            base_sha="bd708385b2b0862b59f53717830a11a8265fb94f",
            head_sha="b8dc1bb7553e2c212b32922866f7cc1a8198fd43",
        )
        assert verdict.decision == "NOT_FIXED"
        assert "Test + Coverage" in verdict.still_failing

    def test_flake_cell_replay_blocks(self):
        """Flake cell ran 4003a6b6: TL fixed Test+Coverage, but engineer's
        edit broke a previously-green Lint check on the same head.
        With the gate: REGRESSION → no ship."""
        verdict = decide(
            base_checks={
                "Lint": _check("Lint", "success"),
                "Test + Coverage": _check("Test + Coverage", "failure"),
            },
            head_checks={
                "Lint": _check("Lint", "failure"),
                "Test + Coverage": _check("Test + Coverage", "success"),
            },
            base_sha="1fa6f0808d9b238d6c8113428a7aea8e30412ad7",
            head_sha="5dd28ac8aa747ea3899b54f361996a2e04736880",
        )
        assert verdict.decision == "REGRESSION"
        assert "Lint" in verdict.regressed
        assert "Test + Coverage" in verdict.fixed
