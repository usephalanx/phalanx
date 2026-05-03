"""Helpers to programmatically generate medium/large CI log + repo variants.

Real-world CI logs aren't 15 lines. They're hundreds to thousands of lines
of pytest collection, warnings, slow test reports, coverage tables. The
real test of TL grounding is: can it find the relevant failure line in a
noisy log? These helpers build plausible noise around a focused failure
so we can measure that.

Real-world repos aren't 3 files. They have dozens of modules, helper
files, conftest.py, docs/. Same question for repos: can TL still pick
the right file when there are 30 candidates?

These helpers don't aim for byte-perfect realism — they aim for the
SHAPE that real CI runs produce.
"""

from __future__ import annotations


def pytest_collection_block(test_count: int, timestamp_prefix: str = "2026-05-01T08:14:11") -> str:
    """Mimics pytest's `collected N items` plus `verbose -v` per-test PASS lines."""
    out = [
        f"{timestamp_prefix}.001Z + python -m pytest tests/ -xvs",
        f"{timestamp_prefix}.234Z =================== test session starts ===================",
        f"{timestamp_prefix}.234Z platform linux -- Python 3.11.9, pytest-8.2.2, pluggy-1.5.0",
        f"{timestamp_prefix}.234Z rootdir: /work",
        f"{timestamp_prefix}.234Z configfile: pyproject.toml",
        f"{timestamp_prefix}.235Z plugins: cov-5.0.0, freezegun-0.4.2, timeout-2.3.1",
        f"{timestamp_prefix}.241Z collecting ... collected {test_count} items",
        "",
    ]
    test_modules = ["test_number", "test_filesize", "test_i18n", "test_lists", "test_words"]
    per_module = max(1, test_count // (len(test_modules) + 1))  # +1 for test_time below
    for mod in test_modules:
        for i in range(per_module):
            out.append(
                f"{timestamp_prefix}.{300 + i:03d}Z tests/{mod}.py::test_{mod[5:]}_case_{i:02d} PASSED [{i:>3}%]"
            )
    return "\n".join(out)


def pytest_warnings_block(timestamp_prefix: str = "2026-05-01T08:14:12") -> str:
    """Realistic deprecation/runtime warnings that surround a failure."""
    return "\n".join([
        f"{timestamp_prefix}.100Z =============================== warnings summary ===============================",
        f"{timestamp_prefix}.100Z tests/test_filesize.py::test_naturalsize_negative",
        f"{timestamp_prefix}.100Z   /work/src/humanize/filesize.py:34: DeprecationWarning: gnu argument is deprecated, use binary",
        f"{timestamp_prefix}.100Z     return _format_size(value, gnu=gnu)",
        f"{timestamp_prefix}.101Z tests/test_number.py::test_intword_large",
        f"{timestamp_prefix}.101Z   /work/src/humanize/number.py:218: RuntimeWarning: large number precision loss",
        f"{timestamp_prefix}.101Z     value = float(value)",
        f"{timestamp_prefix}.101Z -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html",
    ])


def pytest_slow_tests_block(timestamp_prefix: str = "2026-05-01T08:14:13") -> str:
    """Pytest --durations output sometimes appears before the failure summary."""
    slow_tests = [
        ("0.65s", "tests/test_filesize.py::test_naturalsize_petabytes"),
        ("0.42s", "tests/test_lists.py::test_natural_list_50_items"),
        ("0.31s", "tests/test_number.py::test_intword_googolplex"),
        ("0.28s", "tests/test_i18n.py::test_german_translations"),
        ("0.24s", "tests/test_time.py::test_naturaldate_tz_aware"),
    ]
    out = [
        f"{timestamp_prefix}.001Z =============================== slowest 10 durations ===============================",
    ]
    for dur, name in slow_tests:
        out.append(f"{timestamp_prefix}.001Z {dur} call     {name}")
    out.append(f"{timestamp_prefix}.001Z (5 durations < 0.005s hidden.  Use -vv to show these durations.)")
    return "\n".join(out)


def coverage_summary_block(timestamp_prefix: str = "2026-05-01T08:14:14") -> str:
    """Pytest-cov tail that often appears in real CI output."""
    rows = [
        ("src/humanize/__init__.py", "12", "0", "100%"),
        ("src/humanize/filesize.py", "47", "3", "94%"),
        ("src/humanize/i18n.py", "31", "5", "84%"),
        ("src/humanize/lists.py", "22", "2", "91%"),
        ("src/humanize/number.py", "89", "12", "87%"),
        ("src/humanize/time.py", "72", "8", "89%"),
        ("src/humanize/words.py", "18", "1", "94%"),
    ]
    out = [
        f"{timestamp_prefix}.001Z ---------- coverage: platform linux, python 3.11.9 ----------",
        f"{timestamp_prefix}.001Z Name                                Stmts   Miss  Cover",
        f"{timestamp_prefix}.001Z -------------------------------------------------------",
    ]
    total_stmts = total_miss = 0
    for name, stmts, miss, cov in rows:
        out.append(
            f"{timestamp_prefix}.001Z {name:<35} {stmts:>5}  {miss:>5}  {cov:>5}"
        )
        total_stmts += int(stmts)
        total_miss += int(miss)
    out.append(f"{timestamp_prefix}.001Z -------------------------------------------------------")
    out.append(
        f"{timestamp_prefix}.001Z TOTAL                               "
        f"{total_stmts:>5}  {total_miss:>5}  "
        f"{int((total_stmts - total_miss) / total_stmts * 100):>4}%"
    )
    return "\n".join(out)


def synthetic_module_stub(name: str, n_funcs: int = 3) -> str:
    """Generate a plausible Python module stub. Just function defs; no real
    logic. Used to fill out a 'medium' or 'large' repo with unrelated
    files that don't actually carry the bug."""
    lines = [f'"""{name} — humanize submodule."""', "", "from __future__ import annotations", ""]
    for i in range(n_funcs):
        lines.append("")
        lines.append(f"def {name}_helper_{i}(value):")
        lines.append(f'    """Compute something for {name}."""')
        lines.append("    if value is None:")
        lines.append('        raise ValueError("value is required")')
        lines.append("    return str(value)")
    return "\n".join(lines)


def synthetic_test_stub(module_name: str, n_tests: int = 5) -> str:
    """Generate a plausible test module stub. Imports from the module and
    has a few smoke tests that all pass."""
    lines = [
        f'"""Tests for humanize.{module_name}."""',
        "",
        "import pytest",
        f"from humanize.{module_name} import "
        + ", ".join(f"{module_name}_helper_{i}" for i in range(min(n_tests, 3))),
        "",
    ]
    for i in range(n_tests):
        helper = f"{module_name}_helper_{i % 3}"
        lines.append("")
        lines.append(f"def test_{module_name}_case_{i:02d}():")
        lines.append(f'    assert {helper}("x") == "x"')
        lines.append("")
    return "\n".join(lines)
