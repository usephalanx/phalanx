"""Per-provider token pricing and cost computation.

Prices are per-million tokens (USD). Keep them in one place so a single
constant change updates every downstream cost calculation — audit item E
(observability) hinges on accurate per-run cost reporting.

Update policy: when a provider changes pricing, edit the constants here,
bump CI Fixer spec §8 to note the revision, and run the simulation scorecard
so historic fixtures don't regress on cost metrics.
"""

from __future__ import annotations

from phalanx.ci_fixer_v2.context import CostRecord

# ── GPT-5.4 (main agent reasoning) ────────────────────────────────────────
GPT_5_4_INPUT_PER_M_USD: float = 3.00
GPT_5_4_OUTPUT_PER_M_USD: float = 15.00
GPT_5_4_REASONING_PER_M_USD: float = 15.00

# ── Claude Sonnet 4.6 (coder subagent) ────────────────────────────────────
SONNET_4_6_INPUT_PER_M_USD: float = 3.00
SONNET_4_6_OUTPUT_PER_M_USD: float = 15.00
SONNET_4_6_THINKING_PER_M_USD: float = 15.00


def compute_gpt_cost_usd(
    input_tokens: int, output_tokens: int, reasoning_tokens: int
) -> float:
    return (
        input_tokens * GPT_5_4_INPUT_PER_M_USD
        + output_tokens * GPT_5_4_OUTPUT_PER_M_USD
        + reasoning_tokens * GPT_5_4_REASONING_PER_M_USD
    ) / 1_000_000


def compute_sonnet_cost_usd(
    input_tokens: int, output_tokens: int, thinking_tokens: int
) -> float:
    return (
        input_tokens * SONNET_4_6_INPUT_PER_M_USD
        + output_tokens * SONNET_4_6_OUTPUT_PER_M_USD
        + thinking_tokens * SONNET_4_6_THINKING_PER_M_USD
    ) / 1_000_000


def finalize_cost_record(cost: CostRecord) -> CostRecord:
    """Populate the per-provider `cost_usd` fields on a CostRecord from
    its token counters. Returns the same object for chain-call style.
    Safe to call repeatedly — it only reads tokens + writes USD fields.
    """
    cost.gpt_reasoning_cost_usd = compute_gpt_cost_usd(
        cost.gpt_reasoning_input_tokens,
        cost.gpt_reasoning_output_tokens,
        cost.gpt_reasoning_thinking_tokens,
    )
    cost.sonnet_coder_cost_usd = compute_sonnet_cost_usd(
        cost.sonnet_coder_input_tokens,
        cost.sonnet_coder_output_tokens,
        cost.sonnet_coder_thinking_tokens,
    )
    return cost
