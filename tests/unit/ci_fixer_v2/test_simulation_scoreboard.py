"""Unit tests for simulation.scoreboard — aggregation + rendering."""

from __future__ import annotations

import json

from phalanx.ci_fixer_v2.simulation.scoreboard import (
    BEHAVIORAL_GATE,
    LENIENT_GATE,
    aggregate,
    render_json,
    render_markdown,
)
from phalanx.ci_fixer_v2.simulation.scoring import FixtureScore


def _score(
    fixture_id: str,
    language: str = "python",
    failure_class: str = "lint",
    strict: bool = False,
    lenient: bool = True,
    behavioral: bool = True,
    turns: int = 5,
    cost: float = 0.1,
) -> FixtureScore:
    return FixtureScore(
        fixture_id=fixture_id,
        language=language,
        failure_class=failure_class,
        strict=strict,
        lenient=lenient,
        behavioral=behavioral,
        strict_similarity=1.0 if strict else 0.0,
        decision_class_predicted="code_change",
        decision_class_expected="code_change",
        turns_used=turns,
        total_cost_usd=cost,
        verdict="committed",
    )


def test_aggregate_groups_by_language_and_class():
    scores = [
        _score("a", language="python", failure_class="lint"),
        _score("b", language="python", failure_class="lint"),
        _score("c", language="python", failure_class="test_fail"),
        _score("d", language="java", failure_class="lint"),
    ]
    board = aggregate(scores)
    keys = {(r.language, r.failure_class) for r in board.rows}
    assert keys == {
        ("python", "lint"),
        ("python", "test_fail"),
        ("java", "lint"),
    }
    assert sum(r.total for r in board.rows) == 4


def test_aggregate_computes_pass_rates():
    scores = [
        _score("a", lenient=True, behavioral=True, strict=True),
        _score("b", lenient=True, behavioral=True, strict=False),
        _score("c", lenient=False, behavioral=False, strict=False),
        _score("d", lenient=True, behavioral=True, strict=False),
    ]
    board = aggregate(scores)
    row = board.rows[0]
    assert row.total == 4
    assert row.strict_pass == 1
    assert row.lenient_pass == 3
    assert row.behavioral_pass == 3
    assert row.strict_rate == 0.25
    assert row.lenient_rate == 0.75
    assert row.behavioral_rate == 0.75


def test_aggregate_flags_mvp_gates_pass_when_all_exceed_threshold():
    # 100 lenient, 99 behavioral, 99 strict — should pass both gates.
    scores = [
        _score(f"s{i}", lenient=True, behavioral=(i != 0), strict=False)
        for i in range(100)
    ]
    board = aggregate(scores)
    row = board.rows[0]
    assert row.lenient_rate == 1.0
    assert row.behavioral_rate == 0.99
    assert row.mvp_lenient_gate is True
    assert row.mvp_behavioral_gate is True
    assert row.mvp_gates_pass is True


def test_aggregate_fails_gate_when_below_threshold():
    # Only 90% lenient (below 95% gate).
    scores = [
        _score(f"s{i}", lenient=(i < 90), behavioral=True)
        for i in range(100)
    ]
    board = aggregate(scores)
    row = board.rows[0]
    assert row.mvp_lenient_gate is False
    assert row.mvp_behavioral_gate is True
    assert row.mvp_gates_pass is False
    assert "python/lint" in board.failing_rows


def test_aggregate_failing_rows_lists_only_failures():
    scores = [
        _score("ok-1", failure_class="lint", lenient=True, behavioral=True),
        _score("bad-1", failure_class="test_fail", lenient=False, behavioral=False),
    ]
    board = aggregate(scores)
    assert board.failing_rows == ["python/test_fail"]


def test_aggregate_empty_scores():
    board = aggregate([])
    assert board.rows == []
    assert board.total_fixtures == 0
    assert board.mvp_gates_pass is False


def test_aggregate_median_metrics():
    scores = [
        _score("a", turns=3, cost=0.05),
        _score("b", turns=7, cost=0.15),
        _score("c", turns=5, cost=0.10),
    ]
    board = aggregate(scores)
    row = board.rows[0]
    assert row.median_turns == 5.0
    assert row.median_cost_usd == 0.10


def test_gate_constants_match_spec():
    # Spec §12 pins these exactly.
    assert LENIENT_GATE == 0.95
    assert BEHAVIORAL_GATE == 0.99


def test_render_markdown_has_header_rows_and_gate_status():
    scores = [_score("a"), _score("b")]
    board = aggregate(scores)
    md = render_markdown(board)
    assert "Phalanx CI Fixer v2 — Simulation Scoreboard" in md
    assert "Per-row breakdown" in md
    assert "| Language | Class | N | Strict | Lenient | Behavioral | Gates |" in md
    assert "python" in md
    assert "PASS" in md or "FAIL" in md


def test_render_markdown_reports_gate_fail_with_rows():
    scores = [_score("bad", lenient=False, behavioral=False)]
    board = aggregate(scores)
    md = render_markdown(board)
    assert "FAIL" in md
    assert "python/lint" in md


def test_render_json_roundtrip_includes_gates_and_rates():
    scores = [_score("a"), _score("b", lenient=False)]
    board = aggregate(scores)
    out = render_json(board)
    parsed = json.loads(out)
    assert "rows" in parsed
    assert "gates" in parsed
    assert parsed["gates"]["lenient_gate"] == LENIENT_GATE
    assert parsed["gates"]["behavioral_gate"] == BEHAVIORAL_GATE
    assert parsed["total_fixtures"] == 2
    assert "overall_lenient_rate" in parsed
