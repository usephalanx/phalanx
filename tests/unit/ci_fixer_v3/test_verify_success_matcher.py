"""Tier-1 tests for the v1.5.0 verify_success matcher.

Locks in the engineer-side gate that interprets sandbox exec results.
The matcher must:
  - default to exit_code == 0 when criteria is None (backwards compat)
  - allow listed exit_codes (handles "delete test" → pytest exit 4)
  - require stdout_contains substring when present
  - reject when stderr_excludes substring present

Bug #16 root cause: rigid exit==0 gate. v1.5.0 fix: matcher reads from
TL's verify_success criteria, propagated through AgentContext.
"""

from __future__ import annotations

from phalanx.ci_fixer_v2.tools.action import _verify_success_matches


def test_none_criteria_falls_back_to_exit_zero():
    """Backwards compat: pre-v1.5.0 fix_specs without verify_success
    use exit_code == 0 as the gate, matching v1.4.x behavior."""
    assert _verify_success_matches(exit_code=0, stdout="", stderr="", criteria=None) is True
    assert _verify_success_matches(exit_code=1, stdout="", stderr="", criteria=None) is False
    assert _verify_success_matches(exit_code=4, stdout="", stderr="", criteria=None) is False


def test_exit_codes_list_allows_pytest_no_tests_collected():
    """Bug #16 case: TL emits verify_success.exit_codes=[0,4,5] for a
    'delete broken test' fix. pytest exit 4 (no tests collected) should
    flip the gate to True."""
    criteria = {"exit_codes": [0, 4, 5]}
    assert _verify_success_matches(exit_code=4, stdout="", stderr="", criteria=criteria) is True
    assert _verify_success_matches(exit_code=0, stdout="", stderr="", criteria=criteria) is True
    assert _verify_success_matches(exit_code=1, stdout="", stderr="", criteria=criteria) is False


def test_empty_exit_codes_list_falls_back_to_default_zero():
    """Defensive: empty exit_codes treated as [0]."""
    assert (
        _verify_success_matches(exit_code=0, stdout="", stderr="", criteria={"exit_codes": []})
        is True
    )
    assert (
        _verify_success_matches(exit_code=1, stdout="", stderr="", criteria={"exit_codes": []})
        is False
    )


def test_stdout_contains_required_substring():
    criteria = {"exit_codes": [0], "stdout_contains": "All checks passed"}
    assert (
        _verify_success_matches(
            exit_code=0, stdout="All checks passed!", stderr="", criteria=criteria
        )
        is True
    )
    assert (
        _verify_success_matches(exit_code=0, stdout="something else", stderr="", criteria=criteria)
        is False
    )


def test_stderr_excludes_forbidden_substring():
    criteria = {"exit_codes": [0], "stderr_excludes": "DeprecationWarning"}
    assert _verify_success_matches(exit_code=0, stdout="", stderr="", criteria=criteria) is True
    assert (
        _verify_success_matches(
            exit_code=0, stdout="", stderr="some DeprecationWarning here", criteria=criteria
        )
        is False
    )


def test_combined_matchers_all_must_hold():
    """Belt-and-suspenders fix where exit code, stdout and stderr all
    matter."""
    criteria = {
        "exit_codes": [0],
        "stdout_contains": "version 2.10",
        "stderr_excludes": "ImportError",
    }
    assert (
        _verify_success_matches(
            exit_code=0,
            stdout="pydantic version 2.10.4",
            stderr="",
            criteria=criteria,
        )
        is True
    )
    # exit code wrong
    assert (
        _verify_success_matches(
            exit_code=1,
            stdout="pydantic version 2.10.4",
            stderr="",
            criteria=criteria,
        )
        is False
    )
    # stdout missing required substring
    assert (
        _verify_success_matches(
            exit_code=0,
            stdout="pydantic version 2.9.0",
            stderr="",
            criteria=criteria,
        )
        is False
    )
    # stderr has forbidden substring
    assert (
        _verify_success_matches(
            exit_code=0,
            stdout="pydantic version 2.10.4",
            stderr="ImportError: foo",
            criteria=criteria,
        )
        is False
    )


def test_none_stdout_stderr_treated_as_empty():
    """Defensive against ExecResult with stdout=None / stderr=None.
    The matcher mustn't crash on truthy-None checks."""
    criteria = {"exit_codes": [0], "stdout_contains": "x"}
    assert (
        _verify_success_matches(exit_code=0, stdout=None, stderr=None, criteria=criteria) is False
    )
    criteria_no_match = {"exit_codes": [0]}
    assert (
        _verify_success_matches(exit_code=0, stdout=None, stderr=None, criteria=criteria_no_match)
        is True
    )
