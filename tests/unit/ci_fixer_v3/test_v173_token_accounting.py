"""v1.7.3 post-Phase-2a — token accounting fix.

Phase 2a entries E1 (pylint) + E3 (poetry) reported phalanx_cost_usd=$0
in the shadow ledger even though TL ran with multiple LLM tool calls.
Tracing: _tokens_used_from_ctx in cifix_techlead.py + cifix_challenger.py
read non-existent CostRecord fields ('total_tokens', 'input_tokens',
'output_tokens') and always returned 0. The TL/Challenger investigation
loops also didn't accumulate response tokens into ctx.cost (mirroring
v2's run_ci_fix_v2 main loop pattern at ci_fixer_v2/agent.py:118-120).

Net effect: any run that didn't hit the agentic SRE gap-fill (which
DID populate Task.tokens_used) reported $0 cost regardless of LLM
spend. The shadow ledger's aggregate cost was undercounted by every
TL-driven contribution.

This test suite locks the fix:
  - _tokens_used_from_ctx sums the right CostRecord fields
  - Both TL and Challenger versions return identical values
  - Defensive: missing ctx.cost returns 0
"""

from __future__ import annotations

from phalanx.agents.cifix_techlead import _tokens_used_from_ctx as tl_tokens
from phalanx.agents.cifix_challenger import _tokens_used_from_ctx as ch_tokens
from phalanx.ci_fixer_v2.context import CostRecord


class _FakeCtx:
    def __init__(self, cost: CostRecord | None = None):
        self.cost = cost  # may be None to test the defensive branch


# ── _tokens_used_from_ctx — TL + Challenger share the same shape ──────


class TestTokenAccessor:
    def test_sums_all_six_buckets(self):
        cost = CostRecord(
            gpt_reasoning_input_tokens=1000,
            gpt_reasoning_output_tokens=500,
            gpt_reasoning_thinking_tokens=200,
            sonnet_coder_input_tokens=300,
            sonnet_coder_output_tokens=150,
            sonnet_coder_thinking_tokens=50,
        )
        ctx = _FakeCtx(cost)
        # 1000 + 500 + 200 + 300 + 150 + 50
        assert tl_tokens(ctx) == 2200
        assert ch_tokens(ctx) == 2200

    def test_gpt_only_run(self):
        """TL-driven run: only gpt_reasoning_* populated. Both
        accessors return the same total."""
        cost = CostRecord(
            gpt_reasoning_input_tokens=12_000,
            gpt_reasoning_output_tokens=4_000,
            gpt_reasoning_thinking_tokens=2_000,
        )
        ctx = _FakeCtx(cost)
        assert tl_tokens(ctx) == 18_000
        assert ch_tokens(ctx) == 18_000

    def test_sonnet_only_run(self):
        """Challenger-driven run: only sonnet_coder_* populated."""
        cost = CostRecord(
            sonnet_coder_input_tokens=8_000,
            sonnet_coder_output_tokens=2_000,
            sonnet_coder_thinking_tokens=1_000,
        )
        ctx = _FakeCtx(cost)
        assert tl_tokens(ctx) == 11_000
        assert ch_tokens(ctx) == 11_000

    def test_empty_cost_record_returns_zero(self):
        ctx = _FakeCtx(CostRecord())
        assert tl_tokens(ctx) == 0
        assert ch_tokens(ctx) == 0

    def test_no_cost_attribute_returns_zero(self):
        """Defensive: ctx without a cost attribute (rare unit-test
        path) shouldn't crash."""
        class Bare:
            pass

        ctx = Bare()
        assert tl_tokens(ctx) == 0
        assert ch_tokens(ctx) == 0

    def test_cost_is_none_returns_zero(self):
        ctx = _FakeCtx(None)
        assert tl_tokens(ctx) == 0
        assert ch_tokens(ctx) == 0


# ── Lock the regression: old behavior was always 0 even with real tokens ─


class TestRegressionLock:
    """Without the fix, _tokens_used_from_ctx looked for fields named
    'total_tokens' / 'input_tokens' / 'output_tokens' on CostRecord.
    Those don't exist — CostRecord has prefixed buckets like
    gpt_reasoning_input_tokens. The accessor always returned 0 for
    real runs.

    This test would fail under the old implementation (would return
    0 instead of 18_000), and confirms the fix reads the right fields."""

    def test_real_world_tl_run_no_longer_zero(self):
        # Phase 2a E1 pylint: TL did 8 tool calls; tokens populated
        # in ctx.cost.gpt_reasoning_*. Old accessor returned 0;
        # new accessor sums the buckets.
        cost = CostRecord(
            gpt_reasoning_input_tokens=10_000,
            gpt_reasoning_output_tokens=3_000,
            gpt_reasoning_thinking_tokens=5_000,
        )
        ctx = _FakeCtx(cost)
        # Pre-fix: returned 0 (looked for cost.total_tokens — doesn't exist)
        # Post-fix: sums the prefixed buckets
        assert tl_tokens(ctx) > 0
        assert tl_tokens(ctx) == 18_000
