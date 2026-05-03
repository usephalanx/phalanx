"""Tier-1 tests for v1.7.2.3 structured escalation record.

The record is the human-facing audit trail when a CI fix run terminates
non-green. Built deterministically from task rows by build_escalation_record.
"""

from __future__ import annotations

from phalanx.agents._escalation_record import build_escalation_record


# ─────────────────────────────────────────────────────────────────────────────
# Single iteration shape
# ─────────────────────────────────────────────────────────────────────────────


def _setup_task(seq: int = 1) -> dict:
    return {
        "sequence_num": seq,
        "agent_role": "cifix_sre_setup",
        "status": "COMPLETED",
        "output": {
            "mode": "setup",
            "container_id": "abc123",
            "workspace_path": "/tmp/ws",
        },
        "error": None,
    }


def _tl_task(seq: int = 2, **overrides) -> dict:
    base = {
        "root_cause": "long line in foo.py",
        "fix_spec": "wrap the line",
        "affected_files": ["src/foo.py"],
        "verify_command": "ruff check src/foo.py",
        "verify_success": {"exit_codes": [0]},
        "confidence": 0.9,
        "open_questions": [],
    }
    base.update(overrides)
    return {
        "sequence_num": seq,
        "agent_role": "cifix_techlead",
        "status": "COMPLETED",
        "output": base,
        "error": None,
    }


def _eng_task(seq: int = 4, **overrides) -> dict:
    base = {
        "committed": True,
        "commit_sha": "abc1234567890def",
        "files_modified": ["src/foo.py"],
        "v17_path": True,
    }
    base.update(overrides)
    return {
        "sequence_num": seq,
        "agent_role": "cifix_engineer",
        "status": "COMPLETED",
        "output": base,
        "error": None,
    }


def _verify_task(seq: int = 5, **overrides) -> dict:
    base = {
        "mode": "verify",
        "verdict": "new_failures",
        "verify_command": "ruff check src/foo.py",
        "verify_scope": "narrow_from_tl",
        "engineer_commit_sha": "abc1234567890def",
        "verified_commit_sha": "abc1234567890def",
        "fingerprint": "deadbeef12345678",
        "new_failures": [{
            "cmd": "ruff check src/foo.py",
            "exit_code": 1,
            "stderr_tail": "",
            "stdout_tail": "src/foo.py:5: E501",
        }],
    }
    base.update(overrides)
    return {
        "sequence_num": seq,
        "agent_role": "cifix_sre_verify",
        "status": "COMPLETED",
        "output": base,
        "error": None,
    }


class TestBasicShape:
    def test_empty_tasks_returns_empty_record(self):
        record = build_escalation_record(final_reason="test", tasks=[])
        assert record["final_reason"] == "test"
        assert record["iterations"] == []
        assert record["n_iterations"] == 0

    def test_single_iteration_full_chain(self):
        record = build_escalation_record(
            final_reason="iterations_exhausted",
            tasks=[_setup_task(1), _tl_task(2), _eng_task(4), _verify_task(5)],
        )
        assert record["n_iterations"] == 1
        it = record["iterations"][0]
        assert it["iter"] == 1
        assert it["tl"]["root_cause"] == "long line in foo.py"
        assert it["tl"]["confidence"] == 0.9
        assert it["engineer"]["commit_sha"] == "abc1234567890def"
        assert it["verify"]["verdict"] == "new_failures"
        assert it["verify"]["fingerprint"] == "deadbeef12345678"

    def test_three_iteration_run(self):
        tasks = [
            _setup_task(1),
            _tl_task(2, confidence=0.94),
            _eng_task(4, commit_sha="aaa"),
            _verify_task(5, fingerprint="fp1"),
            _tl_task(6, confidence=0.88),
            _eng_task(7, commit_sha="bbb"),
            _verify_task(8, fingerprint="fp2"),
            _tl_task(9, confidence=0.40),
            _eng_task(10, committed=False, skipped_reason="low_confidence"),
            _verify_task(11, fingerprint="fp3"),
        ]
        record = build_escalation_record(
            final_reason="iterations_exhausted", tasks=tasks
        )
        assert record["n_iterations"] == 3
        assert [it["tl"]["confidence"] for it in record["iterations"]] == [0.94, 0.88, 0.40]
        assert [it["verify"]["fingerprint"] for it in record["iterations"]] == ["fp1", "fp2", "fp3"]
        assert record["iterations"][2]["engineer"]["committed"] is False


class TestPatchSafetyEscalation:
    def test_patch_safety_violation_surfaces_prominently(self):
        eng_with_violation = {
            "sequence_num": 4,
            "agent_role": "cifix_engineer",
            "status": "FAILED",
            "output": {
                "committed": False,
                "failed_step": {
                    "step_id": 1,
                    "action": "replace",
                    "error": "patch_safety_violation:blocked_path",
                    "detail": "path '.github/workflows/ci.yml' matches blocked",
                },
            },
            "error": "patch_safety_violation",
        }
        record = build_escalation_record(
            final_reason="patch_safety_violation",
            tasks=[_setup_task(1), _tl_task(2), eng_with_violation],
        )
        assert record["n_iterations"] == 1
        eng = record["iterations"][0]["engineer"]
        assert eng["patch_safety_violation"] == {
            "rule": "blocked_path",
            "detail": "path '.github/workflows/ci.yml' matches blocked",
        }


class TestShaMismatchEscalation:
    def test_sha_mismatch_visible_in_verify_summary(self):
        verify_with_drift = _verify_task(
            5,
            engineer_commit_sha="aaaaaaaaaaaaaaaa",
            verified_commit_sha="bbbbbbbbbbbbbbbb",
            verdict="all_green",
        )
        record = build_escalation_record(
            final_reason="untrusted_green_sha_mismatch",
            tasks=[_setup_task(1), _tl_task(2), _eng_task(4), verify_with_drift],
        )
        v = record["iterations"][0]["verify"]
        assert v["engineer_commit_sha"] == "aaaaaaaaaaaaaaaa"
        assert v["verified_commit_sha"] == "bbbbbbbbbbbbbbbb"


class TestPartialRecord:
    def test_iteration_with_no_engineer_still_recorded(self):
        """If TL ran but engineer never did (mid-iteration crash), the
        iteration record still exists with engineer=None."""
        record = build_escalation_record(
            final_reason="test",
            tasks=[_setup_task(1), _tl_task(2)],
        )
        assert record["n_iterations"] == 1
        assert record["iterations"][0]["tl"] is not None
        assert record["iterations"][0]["engineer"] is None
        assert record["iterations"][0]["verify"] is None

    def test_setup_only_no_iterations(self):
        """If only SRE setup ran and the run died, no iteration record
        is produced — there's nothing to escalate about."""
        record = build_escalation_record(
            final_reason="test", tasks=[_setup_task(1)],
        )
        assert record["n_iterations"] == 0


class TestFingerprintHistory:
    def test_repeated_fingerprints_visible_across_iterations(self):
        """No-progress detection is based on fingerprint repeats —
        the escalation record must surface the history clearly."""
        tasks = [
            _setup_task(1),
            _tl_task(2), _eng_task(4), _verify_task(5, fingerprint="same"),
            _tl_task(6), _eng_task(7), _verify_task(8, fingerprint="same"),
        ]
        record = build_escalation_record(
            final_reason="no_progress_detected", tasks=tasks
        )
        fps = [it["verify"]["fingerprint"] for it in record["iterations"]]
        assert fps == ["same", "same"]


class TestChallengerInRecord:
    def test_challenger_verdict_recorded(self):
        challenger_task = {
            "sequence_num": 3,
            "agent_role": "cifix_challenger",
            "status": "COMPLETED",
            "output": {
                "verdict": "accept",
                "objections": [],
                "shadow_mode": True,
            },
            "error": None,
        }
        record = build_escalation_record(
            final_reason="iterations_exhausted",
            tasks=[
                _setup_task(1), _tl_task(2), challenger_task,
                _eng_task(4), _verify_task(5),
            ],
        )
        ch = record["iterations"][0]["challenger"]
        assert ch["verdict"] == "accept"
        assert ch["shadow_mode"] is True


class TestFinalReason:
    def test_final_reason_propagates(self):
        for reason in [
            "no_progress_detected", "iterations_exhausted",
            "cost_cap_exceeded", "runtime_cap_exceeded",
            "untrusted_green_sha_mismatch",
        ]:
            record = build_escalation_record(final_reason=reason, tasks=[])
            assert record["final_reason"] == reason
