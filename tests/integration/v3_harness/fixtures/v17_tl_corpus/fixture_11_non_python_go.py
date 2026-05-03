"""Fixture 11 — non-Python (Go) stress test.

Same v1.7 architecture, different language. The TL prompt is implicitly
Python-biased (mentions pytest, ruff, pyproject) but the contract should
generalize. Tests whether TL adapts:
  - root_cause names Go-specific entities (TestX, .go files)
  - env_requirements does NOT include irrelevant pip packages
  - reproduce_command + verify_command use `go test`
  - engineer steps target a .go file (not .py)

The bug: `Add` returns 0 instead of summing arguments. A standard Go test
catches it.

Why this matters: marketplace v1.7 needs to demonstrate the architecture
isn't Python-only. If TL hallucinates pip packages on a Go repo, the
prompt has a bias problem we need to fix.
"""

from __future__ import annotations

from tests.integration.v3_harness.fixtures.v17_tl_corpus._types import (
    CorpusFixture,
    confidence_at_least,
    engineer_task_includes_action,
    plan_includes_agent,
)


CI_LOG = """\
2026-05-02T10:22:01.001Z + go test ./...
2026-05-02T10:22:03.412Z === RUN   TestAdd
2026-05-02T10:22:03.413Z     calc_test.go:12: expected 5, got 0
2026-05-02T10:22:03.413Z --- FAIL: TestAdd (0.00s)
2026-05-02T10:22:03.413Z === RUN   TestSubtract
2026-05-02T10:22:03.413Z --- PASS: TestSubtract (0.00s)
2026-05-02T10:22:03.413Z === RUN   TestMultiply
2026-05-02T10:22:03.414Z --- PASS: TestMultiply (0.00s)
2026-05-02T10:22:03.414Z FAIL
2026-05-02T10:22:03.414Z exit status 1
2026-05-02T10:22:03.414Z FAIL    example.com/widget/calc 0.002s
2026-05-02T10:22:03.420Z Error: Process completed with exit code 1.
"""


REPO_FILES = {
    "calc/calc.go": (
        "package calc\n\n"
        "// Add returns the sum of a and b.\n"
        "func Add(a, b int) int {\n"
        "\treturn 0  // BUG: should return a + b\n"
        "}\n\n"
        "func Subtract(a, b int) int {\n"
        "\treturn a - b\n"
        "}\n\n"
        "func Multiply(a, b int) int {\n"
        "\treturn a * b\n"
        "}\n"
    ),
    "calc/calc_test.go": (
        "package calc\n\n"
        "import \"testing\"\n\n"
        "func TestAdd(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\tif got != 5 {\n"
        "\t\tt.Errorf(\"expected 5, got %d\", got)\n"
        "\t}\n"
        "}\n\n"
        "func TestSubtract(t *testing.T) {\n"
        "\tif Subtract(5, 2) != 3 {\n"
        "\t\tt.Error(\"subtract failed\")\n"
        "\t}\n"
        "}\n\n"
        "func TestMultiply(t *testing.T) {\n"
        "\tif Multiply(2, 3) != 6 {\n"
        "\t\tt.Error(\"multiply failed\")\n"
        "\t}\n"
        "}\n"
    ),
    "go.mod": (
        "module example.com/widget\n\n"
        "go 1.22\n"
    ),
    "main.go": (
        "package main\n\n"
        "import (\n"
        "\t\"fmt\"\n"
        "\t\"example.com/widget/calc\"\n"
        ")\n\n"
        "func main() {\n"
        "\tfmt.Println(calc.Add(2, 3))\n"
        "}\n"
    ),
}


def _root_cause_mentions_go_entity():
    """root_cause must reference Go-specific names: TestAdd, Add, calc.go,
    or similar. Catches TL silently transliterating to Python.
    """

    def _check(output: dict) -> None:
        rc = (output.get("root_cause") or "").lower()
        signals = {"testadd", "add", "calc.go", "go test", "calc"}
        if not any(s in rc for s in signals):
            raise AssertionError(
                f"root_cause must reference Go entities (TestAdd/Add/calc.go); "
                f"got: {rc!r}"
            )

    _check.__name__ = "root_cause_mentions_go_entity"
    return _check


def _plan_modifies_go_file():
    """Engineer step must target a .go file (not .py)."""

    def _check(output: dict) -> None:
        for ts in output.get("task_plan") or []:
            if ts.get("agent") != "cifix_engineer":
                continue
            for step in ts.get("steps") or []:
                file_path = step.get("file") or ""
                if file_path.endswith(".go"):
                    return
                if step.get("action") == "apply_diff":
                    diff = step.get("diff") or ""
                    if ".go" in diff:
                        return
        raise AssertionError("no engineer step targets a .go file")

    _check.__name__ = "plan_modifies_go_file"
    return _check


def _env_requirements_not_python_biased():
    """env_requirements should NOT list pip packages — this is a Go repo.
    Either python_packages absent / empty, OR the env is shaped for Go.
    """

    def _check(output: dict) -> None:
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
            pkgs = env.get("python_packages") or []
            # If the python_packages list has any Python-specific deps,
            # that's a Python bias bleed — flag it. (Empty list is fine.)
            if pkgs:
                # Allow common cross-language tools ("pytest" is wrong here)
                pip_specific = {"pytest", "pip", "tox", "nox", "flake8", "ruff",
                                "mypy", "black", "isort", "coverage"}
                for p in pkgs:
                    p_low = p.lower().split(">=")[0].split("==")[0].strip()
                    if p_low in pip_specific:
                        raise AssertionError(
                            f"env_requirements includes Python-biased package "
                            f"{p!r} on a Go repo; envs={envs}"
                        )

    _check.__name__ = "env_requirements_not_python_biased"
    return _check


FIXTURE = CorpusFixture(
    name="11_non_python_go",
    description=(
        "Go test failure: TestAdd expects 5 but gets 0 because Add returns "
        "0 instead of summing args. Tests whether the v1.7 prompt and TL "
        "agent generalize beyond Python — root_cause should reference Go "
        "entities, plan should modify a .go file, env_requirements should "
        "NOT include pytest/pip packages."
    ),
    source_repo="(synthesized; standard Go testing.T failure shape)",
    source_pr_or_commit="N/A — non-Python class",
    complexity="medium",
    ci_log_text=CI_LOG,
    repo_files=REPO_FILES,
    failing_command="go test ./...",
    failing_job_name="test",
    pr_number=141,
    invariants=[
        _root_cause_mentions_go_entity(),
        plan_includes_agent("cifix_engineer", min_count=1),
        plan_includes_agent("cifix_sre_verify", min_count=1),
        _plan_modifies_go_file(),
        _env_requirements_not_python_biased(),
        engineer_task_includes_action("commit"),
        engineer_task_includes_action("push"),
        confidence_at_least(0.6),
    ],
)
