"""Unit tests for the per-provider pricing helpers."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2.context import CostRecord
from phalanx.ci_fixer_v2.pricing import (
    GPT_5_4_INPUT_PER_M_USD,
    GPT_5_4_OUTPUT_PER_M_USD,
    GPT_5_4_REASONING_PER_M_USD,
    SONNET_4_6_INPUT_PER_M_USD,
    SONNET_4_6_OUTPUT_PER_M_USD,
    SONNET_4_6_THINKING_PER_M_USD,
    compute_gpt_cost_usd,
    compute_sonnet_cost_usd,
    finalize_cost_record,
)


def test_compute_gpt_cost_usd_math():
    # 1M input, 1M output, 1M reasoning → sum of the three per-M prices.
    cost = compute_gpt_cost_usd(1_000_000, 1_000_000, 1_000_000)
    expected = (
        GPT_5_4_INPUT_PER_M_USD
        + GPT_5_4_OUTPUT_PER_M_USD
        + GPT_5_4_REASONING_PER_M_USD
    )
    assert cost == pytest.approx(expected)


def test_compute_sonnet_cost_usd_math():
    cost = compute_sonnet_cost_usd(1_000_000, 1_000_000, 1_000_000)
    expected = (
        SONNET_4_6_INPUT_PER_M_USD
        + SONNET_4_6_OUTPUT_PER_M_USD
        + SONNET_4_6_THINKING_PER_M_USD
    )
    assert cost == pytest.approx(expected)


def test_zero_tokens_zero_cost():
    assert compute_gpt_cost_usd(0, 0, 0) == 0.0
    assert compute_sonnet_cost_usd(0, 0, 0) == 0.0


def test_finalize_cost_record_populates_usd_fields_from_tokens():
    cost = CostRecord(
        gpt_reasoning_input_tokens=500_000,
        gpt_reasoning_output_tokens=100_000,
        gpt_reasoning_thinking_tokens=200_000,
        sonnet_coder_input_tokens=300_000,
        sonnet_coder_output_tokens=50_000,
        sonnet_coder_thinking_tokens=80_000,
    )
    finalized = finalize_cost_record(cost)

    assert finalized is cost  # mutates and returns the same object

    expected_gpt = (
        500_000 * GPT_5_4_INPUT_PER_M_USD
        + 100_000 * GPT_5_4_OUTPUT_PER_M_USD
        + 200_000 * GPT_5_4_REASONING_PER_M_USD
    ) / 1_000_000
    expected_sonnet = (
        300_000 * SONNET_4_6_INPUT_PER_M_USD
        + 50_000 * SONNET_4_6_OUTPUT_PER_M_USD
        + 80_000 * SONNET_4_6_THINKING_PER_M_USD
    ) / 1_000_000
    assert cost.gpt_reasoning_cost_usd == pytest.approx(expected_gpt)
    assert cost.sonnet_coder_cost_usd == pytest.approx(expected_sonnet)
    assert cost.total_cost_usd == pytest.approx(expected_gpt + expected_sonnet)


def test_finalize_is_idempotent():
    cost = CostRecord(
        gpt_reasoning_input_tokens=100,
        gpt_reasoning_output_tokens=50,
    )
    finalize_cost_record(cost)
    before = cost.total_cost_usd
    finalize_cost_record(cost)
    assert cost.total_cost_usd == pytest.approx(before)
