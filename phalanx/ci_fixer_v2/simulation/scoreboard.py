"""Aggregate FixtureScores into a per-language-per-class scoreboard.

MVP exit gates (spec §12):

    Lenient pass rate >= 95%  AND  Behavioral pass rate >= 99%

A language passes the MVP bar only when ALL its failure classes satisfy
both gates. This module surfaces the per-row gate flags so CI output
can fail the build loudly when a class regresses below the bar.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Iterable

from phalanx.ci_fixer_v2.simulation.scoring import FixtureScore


LENIENT_GATE: float = 0.95
BEHAVIORAL_GATE: float = 0.99


@dataclass
class ScoreboardRow:
    """One (language, failure_class) summary row."""

    language: str
    failure_class: str
    total: int
    strict_pass: int
    lenient_pass: int
    behavioral_pass: int
    strict_rate: float
    lenient_rate: float
    behavioral_rate: float
    median_turns: float
    median_cost_usd: float
    mvp_lenient_gate: bool
    mvp_behavioral_gate: bool

    @property
    def mvp_gates_pass(self) -> bool:
        return self.mvp_lenient_gate and self.mvp_behavioral_gate


@dataclass
class Scoreboard:
    """Full scoreboard — per-row results + top-level summary."""

    rows: list[ScoreboardRow]
    total_fixtures: int
    total_strict_pass: int
    total_lenient_pass: int
    total_behavioral_pass: int
    failing_rows: list[str] = field(default_factory=list)

    @property
    def overall_lenient_rate(self) -> float:
        return self.total_lenient_pass / self.total_fixtures if self.total_fixtures else 0.0

    @property
    def overall_behavioral_rate(self) -> float:
        return self.total_behavioral_pass / self.total_fixtures if self.total_fixtures else 0.0

    @property
    def mvp_gates_pass(self) -> bool:
        return not self.failing_rows and self.total_fixtures > 0

    def to_dict(self) -> dict:
        return {
            "rows": [asdict(r) for r in self.rows],
            "total_fixtures": self.total_fixtures,
            "total_strict_pass": self.total_strict_pass,
            "total_lenient_pass": self.total_lenient_pass,
            "total_behavioral_pass": self.total_behavioral_pass,
            "overall_lenient_rate": round(self.overall_lenient_rate, 3),
            "overall_behavioral_rate": round(self.overall_behavioral_rate, 3),
            "failing_rows": list(self.failing_rows),
            "mvp_gates_pass": self.mvp_gates_pass,
            "gates": {
                "lenient_gate": LENIENT_GATE,
                "behavioral_gate": BEHAVIORAL_GATE,
            },
        }


# ─────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────


def aggregate(scores: Iterable[FixtureScore]) -> Scoreboard:
    """Group FixtureScores by (language, failure_class) and compute rows."""
    scores_list = list(scores)
    buckets: dict[tuple[str, str], list[FixtureScore]] = {}
    for s in scores_list:
        buckets.setdefault((s.language, s.failure_class), []).append(s)

    rows: list[ScoreboardRow] = []
    failing_rows: list[str] = []

    for (lang, cls), group in sorted(buckets.items()):
        total = len(group)
        strict_p = sum(1 for s in group if s.strict)
        lenient_p = sum(1 for s in group if s.lenient)
        behavioral_p = sum(1 for s in group if s.behavioral)
        strict_rate = strict_p / total
        lenient_rate = lenient_p / total
        behavioral_rate = behavioral_p / total
        med_turns = median(s.turns_used for s in group) if group else 0
        med_cost = median(s.total_cost_usd for s in group) if group else 0.0

        lenient_gate = lenient_rate >= LENIENT_GATE
        behavioral_gate = behavioral_rate >= BEHAVIORAL_GATE

        row = ScoreboardRow(
            language=lang,
            failure_class=cls,
            total=total,
            strict_pass=strict_p,
            lenient_pass=lenient_p,
            behavioral_pass=behavioral_p,
            strict_rate=round(strict_rate, 3),
            lenient_rate=round(lenient_rate, 3),
            behavioral_rate=round(behavioral_rate, 3),
            median_turns=float(med_turns),
            median_cost_usd=round(float(med_cost), 4),
            mvp_lenient_gate=lenient_gate,
            mvp_behavioral_gate=behavioral_gate,
        )
        rows.append(row)
        if not row.mvp_gates_pass:
            failing_rows.append(f"{lang}/{cls}")

    return Scoreboard(
        rows=rows,
        total_fixtures=len(scores_list),
        total_strict_pass=sum(1 for s in scores_list if s.strict),
        total_lenient_pass=sum(1 for s in scores_list if s.lenient),
        total_behavioral_pass=sum(1 for s in scores_list if s.behavioral),
        failing_rows=failing_rows,
    )


# ─────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────


def render_markdown(scoreboard: Scoreboard) -> str:
    """Human-readable scoreboard for PR comments and CI logs."""
    lines: list[str] = []
    lines.append("# Phalanx CI Fixer v2 — Simulation Scoreboard")
    lines.append("")
    lines.append(f"Total fixtures: **{scoreboard.total_fixtures}**")
    lines.append(
        f"Overall lenient: **{scoreboard.overall_lenient_rate:.1%}**  "
        f"(gate: {LENIENT_GATE:.0%})"
    )
    lines.append(
        f"Overall behavioral: **{scoreboard.overall_behavioral_rate:.1%}**  "
        f"(gate: {BEHAVIORAL_GATE:.0%})"
    )
    if scoreboard.mvp_gates_pass:
        lines.append("")
        lines.append("**Gate status:** PASS")
    else:
        lines.append("")
        lines.append(
            f"**Gate status:** FAIL — failing rows: "
            f"{', '.join(scoreboard.failing_rows) or '(none)'}"
        )

    lines.append("")
    lines.append("## Per-row breakdown")
    lines.append("")
    lines.append("| Language | Class | N | Strict | Lenient | Behavioral | Gates |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in scoreboard.rows:
        gates = "PASS" if r.mvp_gates_pass else "FAIL"
        lines.append(
            f"| {r.language} | {r.failure_class} | {r.total} | "
            f"{r.strict_rate:.1%} | {r.lenient_rate:.1%} | "
            f"{r.behavioral_rate:.1%} | {gates} |"
        )
    return "\n".join(lines) + "\n"


def render_json(scoreboard: Scoreboard) -> str:
    return json.dumps(scoreboard.to_dict(), indent=2, sort_keys=True)
