"""
Memory Assembler — builds structured prompt blocks from retrieved memory.

Design:
  - Takes facts + decisions from MemoryReader and formats them for Claude injection.
  - Enforces a token budget — trims lower-confidence facts first.
  - Output is a plain-text "memory block" that goes into the agent's system prompt.
  - Stateless — no DB calls; pure transformation of already-loaded data.

Evidence: Context window management is critical for cost and quality.
  Standing decisions first (always in context), then high-confidence facts,
  then recent facts — mirrors how a senior engineer recalls institutional knowledge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phalanx.db.models import MemoryDecision, MemoryFact

# Approximate characters per token (conservative estimate for GPT/Claude)
_CHARS_PER_TOKEN = 4

# Section headers
_DECISIONS_HEADER = "## Architectural Decisions (Always Apply)\n"
_STANDING_FACTS_HEADER = "## Standing Facts (Team Invariants)\n"
_RECENT_FACTS_HEADER = "## Recent Context\n"


class MemoryAssembler:
    """
    Builds the memory prompt block injected into every agent system prompt.

    Usage:
        assembler = MemoryAssembler(max_tokens=4000)
        block = assembler.build(
            decisions=standing_decisions,
            standing_facts=standing_facts,
            recent_facts=recent_facts,
        )
        # block is a string to prepend to the system prompt
    """

    def __init__(self, max_tokens: int = 4000) -> None:
        self.max_tokens = max_tokens
        self._max_chars = max_tokens * _CHARS_PER_TOKEN

    def build(
        self,
        decisions: list[MemoryDecision] | None = None,
        standing_facts: list[MemoryFact] | None = None,
        recent_facts: list[MemoryFact] | None = None,
    ) -> str:
        """
        Assemble the memory block from the provided memory entries.
        Trims lower-priority content if the budget would be exceeded.

        Priority order (highest first):
          1. Architectural decisions (always included while budget allows)
          2. Standing facts (team invariants — include next)
          3. Recent facts (ordered by relevance_score)

        Returns an empty string if nothing is provided.
        """
        parts: list[str] = []
        remaining_chars = self._max_chars

        # ── 1. Decisions ─────────────────────────────────────────────────────
        if decisions:
            section = _DECISIONS_HEADER
            for d in decisions:
                entry = self._format_decision(d)
                if len(section) + len(entry) > remaining_chars * 0.4:
                    break  # reserve 60% for facts
                section += entry
            if section != _DECISIONS_HEADER:
                parts.append(section.rstrip())
                remaining_chars -= len(section)

        # ── 2. Standing facts ─────────────────────────────────────────────────
        if standing_facts:
            section = _STANDING_FACTS_HEADER
            for f in standing_facts:
                entry = self._format_fact(f)
                if len(section) + len(entry) > remaining_chars * 0.5:
                    break
                section += entry
            if section != _STANDING_FACTS_HEADER:
                parts.append(section.rstrip())
                remaining_chars -= len(section)

        # ── 3. Recent facts ───────────────────────────────────────────────────
        if recent_facts:
            # Sort by relevance_score descending
            sorted_recent = sorted(recent_facts, key=lambda f: f.relevance_score, reverse=True)
            section = _RECENT_FACTS_HEADER
            for f in sorted_recent:
                entry = self._format_fact(f)
                if len(section) + len(entry) > remaining_chars:
                    break
                section += entry
            if section != _RECENT_FACTS_HEADER:
                parts.append(section.rstrip())

        if not parts:
            return ""

        return "# Project Memory\n\n" + "\n\n".join(parts) + "\n"

    @staticmethod
    def _format_decision(d: MemoryDecision) -> str:
        lines = [f"**{d.title}**", d.decision]
        if d.rationale:
            lines.append(f"_Rationale:_ {d.rationale}")
        if d.rejected_alternatives:
            alts = ", ".join(d.rejected_alternatives)
            lines.append(f"_Rejected:_ {alts}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _format_fact(f: MemoryFact) -> str:
        confidence_str = f" _(confidence: {f.confidence:.0%})_" if f.confidence < 1.0 else ""
        return f"- **[{f.fact_type}] {f.title}**{confidence_str}: {f.body}\n"
