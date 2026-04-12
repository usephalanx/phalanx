"""
CI failure classifier — categorizes a CI log into a failure type.

Used by CIFixerAgent to choose the right fix strategy.
Each category maps to a different prompt approach:
  - test:       fix implementation to satisfy assertions
  - lint:       fix exactly the lines flagged, nothing else
  - type:       add/fix types or resolve type mismatches
  - build:      fix imports, syntax, missing deps
  - dependency: update lockfile or pin versions
  - unknown:    attempt best-effort fix, low confidence
"""

from __future__ import annotations

import re

# Ordered by specificity — first match wins
_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "lint",
        [
            r"ruff.*error",
            r"pylint.*error",
            r"eslint.*error",
            r"prettier.*error",
            r"Found \d+ error",
            r"error\[E\d+\]",  # ruff error codes
            r"W\d{4}",  # pylint warning codes
            r"Trailing whitespace",
            r"Missing newline at end of file",
        ],
    ),
    (
        "type",
        [
            r"error TS\d+",  # tsc
            r"error: Argument of type",  # mypy
            r"error: Incompatible return value",  # mypy
            r"error: Item .* has no attribute",  # mypy
            r"error: Cannot find name",  # tsc
            r"Type '\w+' is not assignable",  # tsc
            r"Property '\w+' does not exist",  # tsc
            r"mypy.*Found \d+ error",
        ],
    ),
    (
        "test",
        [
            r"FAILED tests/",  # pytest
            r"AssertionError",
            r"assert .* ==",
            r"\d+ failed",
            r"● Test Suites:.*failed",  # jest
            r"FAIL src/",  # jest
            r"Tests:\s+\d+ failed",
            r"Expected:.*\nReceived:",
            r"test.*FAILED",
        ],
    ),
    (
        "build",
        [
            r"SyntaxError",
            r"IndentationError",
            r"ModuleNotFoundError",
            r"ImportError",
            r"Cannot find module",
            r"Module not found",
            r"Failed to compile",
            r"Build failed",
            r"error: cannot open",
            r"make.*Error \d+",
        ],
    ),
    (
        "dependency",
        [
            r"ResolutionImpossible",
            r"Could not find a version",
            r"No matching distribution",
            r"peer dep missing",
            r"Could not resolve dependency",
            r"npm ERR! code ERESOLVE",
            r"yarn error",
        ],
    ),
]


def classify_failure(log_text: str) -> str:
    """
    Return the failure category for a CI log.

    Categories (in priority order):
      lint, type, test, build, dependency, unknown

    Lint and type checks are checked before test because a lint
    failure in CI often appears alongside test output.
    """
    for category, patterns in _PATTERNS:
        if any(re.search(p, log_text, re.IGNORECASE | re.MULTILINE) for p in patterns):
            return category
    return "unknown"


def extract_failing_files(log_text: str) -> list[str]:
    """
    Extract file paths mentioned in CI failure output.

    Handles common formats:
      - pytest:  FAILED tests/unit/test_foo.py::TestBar::test_baz
      - jest:    FAIL src/components/Foo.test.tsx
      - mypy:    src/foo.py:42: error: ...
      - ruff:    src/foo.py:42:5: E501 ...
      - tsc:     src/foo.ts(42,5): error TS2345: ...
    """
    patterns = [
        r"FAILED\s+([\w/\.\-]+\.py)",  # pytest
        r"FAIL\s+([\w/\.\-]+\.[jt]sx?)",  # jest
        r"([\w/\.\-]+\.py):\d+:\s+error",  # mypy/ruff
        r"([\w/\.\-]+\.[jt]sx?)[\(:]\d+",  # tsc/eslint
        r"error in ([\w/\.\-]+\.\w+)",
    ]
    files: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, log_text, re.MULTILINE):
            path = match.group(1)
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files[:10]  # cap at 10 files to keep prompt manageable
