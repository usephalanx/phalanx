"""Fixture 02 — ImportError on a runtime dep missing from pyproject.

Source pattern: real shape from many Python OSS PRs that add a feature
using a new library but forget to add it to install_requires /
[project].dependencies. CI passes locally (dev had it installed) but
fresh-install CI fails.

Why this fixture matters:
  - Validates TL detects that the env is the gap, not the code logic.
  - Validates SRE setup task IS included with the missing pkg.
  - Validates engineer task ALSO updates pyproject.toml (or
    requirements.txt) so the fix is durable, not just sandbox-local.
  - Distinguishes "pure code fix" from "code+env fix" shapes.

What TL should produce:
  - task_plan: [sre_setup with python_packages=[httpx], engineer
    (modifies pyproject.toml + commit + push), sre_verify]
  - env_requirements at top level OR in sre_setup task includes httpx
  - confidence: ≥ 0.7
  - root_cause mentions "httpx" + "ImportError" or "ModuleNotFoundError"
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_includes_agent,
    plan_steps_modify,
    root_cause_mentions,
)


def _httpx_appears_explicitly_or_via_pyproject():
    """Either:
      (a) env_requirements.python_packages contains 'httpx' (explicit), OR
      (b) an engineer step modifies pyproject.toml with 'httpx' in the
          new dependency list (implicit via pip install -e .)
    Both are valid v1.7 fixes — the dep needs to be reachable at verify
    time, however TL achieves it.
    """
    def _check(output: dict) -> None:
        # Path A: explicit
        envs: list[dict] = []
        top = output.get("env_requirements")
        if isinstance(top, dict):
            envs.append(top)
        for ts in output.get("task_plan") or []:
            if ts.get("agent") == "cifix_sre_setup":
                env = ts.get("env_requirements")
                if isinstance(env, dict):
                    envs.append(env)
        for env in envs:
            for p in env.get("python_packages") or []:
                if "httpx" in p.lower():
                    return

        # Path B: pyproject edit with httpx
        for ts in output.get("task_plan") or []:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                if step.get("file") != "pyproject.toml":
                    continue
                for field in ("new", "content"):
                    val = step.get(field) or ""
                    if "httpx" in val:
                        return
                if step.get("action") == "apply_diff":
                    diff = step.get("diff") or ""
                    if "httpx" in diff and "pyproject.toml" in diff:
                        return

        raise AssertionError(
            "neither env_requirements.python_packages nor pyproject.toml "
            "edit declares 'httpx' — sandbox won't have the dep at verify time"
        )
    _check.__name__ = "httpx_reachable_at_verify(env|pyproject)"
    return _check

CI_LOG = """\
2026-04-29T11:02:08.001Z + python -m pip install -e .
2026-04-29T11:02:14.554Z Successfully installed sample-pkg-0.2.3
2026-04-29T11:02:14.892Z + python -m pytest tests/ -q
2026-04-29T11:02:15.122Z ImportError while loading conftest '/work/tests/conftest.py'.
2026-04-29T11:02:15.122Z tests/conftest.py:3: in <module>
2026-04-29T11:02:15.122Z     import httpx
2026-04-29T11:02:15.122Z E   ModuleNotFoundError: No module named 'httpx'
2026-04-29T11:02:15.143Z Error: Process completed with exit code 4.
"""


REPO_FILES = {
    "src/sample_pkg/api.py": (
        '"""API client using httpx (added in this PR but not in deps)."""\n'
        "import httpx\n\n\n"
        "def fetch(url: str) -> str:\n"
        "    return httpx.get(url).text\n"
    ),
    "tests/conftest.py": (
        "import pytest\n"
        "import httpx\n\n\n"
        "@pytest.fixture\n"
        "def http_client():\n"
        "    return httpx.Client()\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"sample-pkg\"\n"
        "version = \"0.2.3\"\n"
        # Note: httpx INTENTIONALLY MISSING from dependencies — that's the bug.
        "dependencies = [\n"
        "  \"requests>=2.28\",\n"
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=7\"]\n"
    ),
}


FIXTURE = CorpusFixture(
    name="02_importerror_missing_dep",
    description=(
        "ImportError because a new dep (httpx) was used in code but never "
        "added to pyproject. Fix has TWO parts: (a) SRE installs httpx in "
        "the sandbox so verify can run; (b) engineer adds httpx to "
        "pyproject.toml dependencies so the fix is durable."
    ),
    source_repo="(synthesized; common shape — e.g., pallets/click PR #2245 "
    "had a similar 'forgot dep' bug 2023-10)",
    source_pr_or_commit="N/A — generic ImportError-after-merge",
    complexity="medium",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="python -m pytest tests/ -q",
    failing_job_name="test",
    pr_number=128,
    invariants=[
        root_cause_mentions("httpx"),
        # SRE setup task is REQUIRED — sandbox needs the test runner provisioned
        plan_includes_agent("cifix_sre_setup", min_count=1),
        # Engineer task is REQUIRED — pyproject.toml needs the dep added
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        # The durable fix lives in pyproject.toml
        plan_steps_modify("pyproject.toml"),
        # httpx must reach the sandbox at verify time — either explicit in
        # env_requirements OR via the pyproject edit + pip install -e .
        _httpx_appears_explicitly_or_via_pyproject(),
        # Engineer must commit + push the pyproject change
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        confidence_at_least(0.7),
    ],
)
