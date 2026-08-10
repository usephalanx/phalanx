"""Probe-synthesis prompt contract — the false-green regression guard.

Receipt `fix-589b5dff378c` certified a fix that granted [0,0,0] seats: the
customer got nothing, including on the legitimate first delivery, and the gate
said `proven`. Two causes, both in `_synth_prompt`:

  1. The prompt TOLD the author that zero was a pass — "a grant that is ...
     skipped ... or produces no record counts as HELD (0)" and "missing/empty =
     the bad thing did NOT happen = HELD". A [0,0,0] series satisfies that
     literally.
  2. The service-app stub recipe said "returns canned rows" with no conflict
     semantics, so `INSERT ... ON CONFLICT DO NOTHING` reported "no row created"
     on the FIRST insert too — suppressing the legitimate grant.

These tests pin both. They assert on prompt CONTENT, which is the only
deterministic surface a prompt change has — the probe itself is authored by a
model, so this proves the instruction is delivered, not that it is obeyed. The
behavioural check is a live run against billing-dogfood expecting [5,5,5].
"""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v3.synthesize_probe_task import _synth_prompt

NODE_ENV = {
    "lang": "JavaScript/Node", "probe_file": "_fs_probe.js",
    "probe_cmd": "node _fs_probe.js", "setup_cmds": [],
    "service": True, "db_hint": "pg",
}
PY_ENV = {
    "lang": "Python", "probe_file": "_fs_probe.py",
    "probe_cmd": "python3 _fs_probe.py", "setup_cmds": [],
    "service": True, "db_hint": "psycopg",
}
PLAIN_ENV = {**NODE_ENV, "service": False, "db_hint": ""}

BUG = "Stripe webhook double-provision: grantSeats runs again on redelivery"


# ── cause 1: the invariant must be exact, and zero must not be a free pass ───


class TestExactInvariant:
    def test_demands_exact_equality_not_a_bound(self):
        p = _synth_prompt(BUG, PLAIN_ENV)
        assert "EXACT expected final state" in p
        assert "`==`" in p

    def test_names_the_degenerate_fixes_that_must_fail(self):
        p = _synth_prompt(BUG, PLAIN_ENV)
        for phrase in ("do-nothing fix", "over-suppressing fix", "ZERO"):
            assert phrase in p, f"prompt no longer rules out: {phrase}"
        assert "OVER-count" in p and "UNDER-count" in p

    def test_zero_is_not_automatically_held(self):
        """The exact instruction that produced the false green."""
        p = _synth_prompt(BUG, PLAIN_ENV)
        assert "'nothing happened' is NOT automatically HELD" in p

    @pytest.mark.parametrize("gone", [
        "produces no record counts as HELD",
        "missing/empty = the bad thing did NOT happen = HELD",
    ])
    def test_false_green_instructions_are_gone(self, gone):
        p = _synth_prompt(BUG, PLAIN_ENV)
        assert gone not in p, f"the instruction that caused fix-589b5dff378c is back: {gone!r}"

    def test_distinguishes_rejection_bugs_from_idempotency_bugs(self):
        """Zero IS correct for a clamp/reject bug and WRONG for a duplicate bug;
        collapsing the two is what made the weak invariant look reasonable."""
        p = _synth_prompt(BUG, PLAIN_ENV)
        assert "REJECTION / CLAMP" in p
        assert "IDEMPOTENCY / DUPLICATE-DELIVERY" in p
        assert "FIRST delivery must still" in p

    def test_graceful_exit_zero_survived_the_rewrite(self):
        """The rewrite must not reintroduce exit-2 flakes on a correct fix."""
        p = _synth_prompt(BUG, PLAIN_ENV)
        assert "never throws, never exits 2" in p
        assert "defensively" in p


# ── cause 2: stub must model ON CONFLICT ─────────────────────────────────────


class TestStubFidelity:
    @pytest.mark.parametrize("env,rowcount", [(NODE_ENV, "rowCount"), (PY_ENV, "rowcount")])
    def test_service_recipe_demands_conflict_semantics(self, env, rowcount):
        p = _synth_prompt(BUG, env)
        assert "CONFLICT SEMANTICS" in p
        assert "ON CONFLICT DO NOTHING" in p
        assert "FIRST insert" in p
        assert rowcount in p, f"language-correct attribute {rowcount} missing"

    @pytest.mark.parametrize("env", [NODE_ENV, PY_ENV])
    def test_names_the_failure_mode_explicitly(self, env):
        p = _synth_prompt(BUG, env)
        assert "always reports 'no row created'" in p
        assert "false green" in p

    def test_conflict_guidance_only_for_service_apps(self):
        """A plain app has no DB stub; the guidance would be noise."""
        assert "CONFLICT SEMANTICS" not in _synth_prompt(BUG, PLAIN_ENV)

    def test_service_block_still_present_for_service_apps(self):
        assert "SERVICE APP" in _synth_prompt(BUG, NODE_ENV)


# ── the prompt must still be coherent ────────────────────────────────────────


class TestPromptIntegrity:
    @pytest.mark.parametrize("env", [NODE_ENV, PY_ENV, PLAIN_ENV])
    def test_core_contract_intact(self, env):
        p = _synth_prompt(BUG, env)
        assert BUG in p
        assert "FS_PROOF_JSON=" in p
        assert "side_effect" in p
        assert env["probe_file"] in p and env["probe_cmd"] in p

    def test_grounding_still_threads_through(self):
        p = _synth_prompt(BUG, PLAIN_ENV, "deliver the same event_id twice")
        assert "KNOWN REPRODUCTION" in p
        assert "deliver the same event_id twice" in p

    def test_exit_code_convention_unchanged(self):
        p = _synth_prompt(BUG, PLAIN_ENV)
        assert "0 = invariant HELD" in p
        assert "1 = invariant VIOLATED" in p
        assert "2 = harness/setup error" in p
