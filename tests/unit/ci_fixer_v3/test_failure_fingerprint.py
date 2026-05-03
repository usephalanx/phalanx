"""Tier-1 tests for v1.7.2.3 failure fingerprint module.

The fingerprint dedupes verify failures across iterations. Two iterations
producing "the same" failure (same command, same exit, same semantic
output) → same hash → commander stops the loop.

The risk is over-normalization: collapsing genuinely-different failures
into the same hash and prematurely killing a run that was progressing.
These tests pin the boundary — what should hash the same vs. what should
hash differently.
"""

from __future__ import annotations

from phalanx.agents._failure_fingerprint import (
    compute_fingerprint,
    is_repeated,
)


class TestFingerprintStability:
    """Same inputs → same hash. Always."""

    def test_identical_inputs_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="ruff check src/foo.py",
            exit_code=1,
            stdout_tail="src/foo.py:5:101: E501 Line too long (104 > 100)",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="ruff check src/foo.py",
            exit_code=1,
            stdout_tail="src/foo.py:5:101: E501 Line too long (104 > 100)",
            stderr_tail="",
        )
        assert fp1 == fp2

    def test_hash_is_16_chars_hex(self):
        fp = compute_fingerprint(cmd="x", exit_code=1, stdout_tail="", stderr_tail="")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)


class TestNormalizationCollapsesNoise:
    """These transformations should NOT change the fingerprint."""

    def test_different_line_numbers_same_hash(self):
        """Pytest tracebacks differ in line numbers between runs (e.g. if
        the patch added a line above). The semantic failure is the same."""
        fp1 = compute_fingerprint(
            cmd="pytest tests/test_x.py",
            exit_code=1,
            stdout_tail="src/foo.py:42: AssertionError: expected 5",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="pytest tests/test_x.py",
            exit_code=1,
            stdout_tail="src/foo.py:43: AssertionError: expected 5",
            stderr_tail="",
        )
        assert fp1 == fp2

    def test_different_tempdirs_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="pytest",
            exit_code=1,
            stdout_tail="ImportError: cannot find /tmp/abc/foo.py",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="pytest",
            exit_code=1,
            stdout_tail="ImportError: cannot find /tmp/xyz/foo.py",
            stderr_tail="",
        )
        assert fp1 == fp2

    def test_different_run_ids_in_path_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="pytest",
            exit_code=1,
            stdout_tail="error in /tmp/forge-repos/v3-aaaa1111-bbbb-engineer/x.py",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="pytest",
            exit_code=1,
            stdout_tail="error in /tmp/forge-repos/v3-cccc2222-dddd-engineer/x.py",
            stderr_tail="",
        )
        assert fp1 == fp2

    def test_different_container_ids_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="ruff check .",
            exit_code=1,
            stdout_tail="",
            stderr_tail="container b89d65bf9dc8 reported error",
        )
        fp2 = compute_fingerprint(
            cmd="ruff check .",
            exit_code=1,
            stdout_tail="",
            stderr_tail="container 4283033d662d reported error",
        )
        assert fp1 == fp2

    def test_different_durations_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="pytest",
            exit_code=1,
            stdout_tail="failed in 1.234s",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="pytest",
            exit_code=1,
            stdout_tail="failed in 0.087s",
            stderr_tail="",
        )
        assert fp1 == fp2

    def test_different_timestamps_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="x", exit_code=1,
            stdout_tail="2026-05-03T10:00:00 ERROR: bad input",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="x", exit_code=1,
            stdout_tail="2026-05-03T10:00:01 ERROR: bad input",
            stderr_tail="",
        )
        assert fp1 == fp2

    def test_whitespace_variation_same_hash(self):
        fp1 = compute_fingerprint(
            cmd="x", exit_code=1, stdout_tail="error  occurred", stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="x", exit_code=1, stdout_tail="error\toccurred", stderr_tail="",
        )
        assert fp1 == fp2


class TestFingerprintDistinguishesGenuineDifferences:
    """These transformations SHOULD change the fingerprint."""

    def test_different_command_different_hash(self):
        fp1 = compute_fingerprint(
            cmd="ruff check src/a.py", exit_code=1, stdout_tail="x", stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="ruff check src/b.py", exit_code=1, stdout_tail="x", stderr_tail="",
        )
        assert fp1 != fp2

    def test_different_exit_code_different_hash(self):
        fp1 = compute_fingerprint(cmd="x", exit_code=1, stdout_tail="y", stderr_tail="")
        fp2 = compute_fingerprint(cmd="x", exit_code=2, stdout_tail="y", stderr_tail="")
        assert fp1 != fp2

    def test_different_error_class_different_hash(self):
        fp1 = compute_fingerprint(
            cmd="pytest", exit_code=1,
            stdout_tail="AssertionError: expected 5", stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="pytest", exit_code=1,
            stdout_tail="ValueError: invalid input", stderr_tail="",
        )
        assert fp1 != fp2

    def test_different_violation_codes_different_hash(self):
        """E501 (line too long) and F401 (unused import) are different
        ruff rules — hashes must distinguish them."""
        fp1 = compute_fingerprint(
            cmd="ruff check src/foo.py", exit_code=1,
            stdout_tail="src/foo.py:5: E501 Line too long",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="ruff check src/foo.py", exit_code=1,
            stdout_tail="src/foo.py:5: F401 unused import",
            stderr_tail="",
        )
        assert fp1 != fp2

    def test_different_files_in_error_different_hash(self):
        fp1 = compute_fingerprint(
            cmd="pytest", exit_code=1,
            stdout_tail="src/a.py:5: AssertionError: foo",
            stderr_tail="",
        )
        fp2 = compute_fingerprint(
            cmd="pytest", exit_code=1,
            stdout_tail="src/b.py:5: AssertionError: foo",
            stderr_tail="",
        )
        assert fp1 != fp2


class TestIsRepeated:
    """The commander's no-progress signal."""

    def test_empty_list_not_repeated(self):
        assert is_repeated([]) is False

    def test_single_fingerprint_not_repeated(self):
        assert is_repeated(["abc123"]) is False

    def test_two_different_not_repeated(self):
        assert is_repeated(["abc123", "def456"]) is False

    def test_two_same_is_repeated(self):
        assert is_repeated(["abc123", "abc123"]) is True

    def test_three_same_is_repeated(self):
        """Iter 3 same as iter 2 — last-pair check catches this."""
        assert is_repeated(["abc123", "abc123", "abc123"]) is True

    def test_three_with_progress_then_regress(self):
        """Iter 1 ≠ iter 2, iter 3 = iter 2 — STILL counts as no-progress
        because the LAST two are the same. This is the right call: a
        rollback to a prior failure shape is itself a non-progress signal.
        """
        assert is_repeated(["a", "b", "b"]) is True

    def test_progress_chain_not_repeated(self):
        assert is_repeated(["a", "b", "c"]) is False
