"""Tier-1 tests for v1.7.1 workflow YAML extractor.

Coverage strategy: each test sets up a tempdir with a hand-crafted
.github/workflows/<file>.yml that mirrors a real-world shape, then
asserts the extractor produces the right shell commands (or correctly
returns None for cases that should fall to Tier 1).

Real-world shapes covered:
  - pytest-with-uv (humanize-style)
  - pytest-with-pip (testbed-style)
  - pre-commit-only (lint workflow)
  - matrix.python-version (template literal — should bail)
  - custom org action (unsupported — should bail)
  - non-matching job name (returns None)
  - missing workflow dir (returns None)
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from phalanx.agents._v171_workflow_extractor import (
    ExtractedRecipe,
    extract_recipe,
    extract_recipe_from_workflow,
    find_workflow_files,
)


@pytest.fixture
def workspace():
    """A tempdir with a `.github/workflows/` directory we'll populate per-test."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / ".github" / "workflows").mkdir(parents=True)
        yield ws


def _write_workflow(ws: Path, name: str, content: str) -> Path:
    """Write a workflow YAML to .github/workflows/<name>."""
    path = ws / ".github" / "workflows" / name
    path.write_text(textwrap.dedent(content))
    return path


# ─── find_workflow_files ─────────────────────────────────────────────────────


class TestFindWorkflowFiles:
    def test_finds_yml_and_yaml(self, workspace):
        _write_workflow(workspace, "test.yml", "name: test\non: push\njobs: {}\n")
        _write_workflow(workspace, "lint.yaml", "name: lint\non: push\njobs: {}\n")
        files = find_workflow_files(workspace)
        assert len(files) == 2
        assert {f.name for f in files} == {"test.yml", "lint.yaml"}

    def test_returns_empty_when_no_workflow_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert find_workflow_files(Path(tmp)) == []

    def test_stable_order(self, workspace):
        _write_workflow(workspace, "z.yml", "jobs: {}\n")
        _write_workflow(workspace, "a.yml", "jobs: {}\n")
        files = find_workflow_files(workspace)
        # Sorted alphabetically — order is stable across runs
        assert [f.name for f in files] == ["a.yml", "z.yml"]


# ─── extract_recipe — happy path shapes ───────────────────────────────────────


class TestPytestWithUv:
    """humanize-style workflow: setup-uv + uv sync + pytest."""

    def test_renders_uv_install_and_test_commands(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            name: test
            on: [push, pull_request]
            jobs:
              test:
                name: test
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: astral-sh/setup-uv@v3
                  - uses: actions/setup-python@v5
                    with:
                      python-version: '3.12'
                  - run: uv sync --extra dev
                  - run: uv run pytest tests/
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        # Expected: uv install command, python version note, sync.
        # The test runner ("uv run pytest tests/") is EXCLUDED — SRE
        # setup must not execute the test during provisioning (v1.7.1.1
        # fix; testbed lint cell broke when ruff was emitted as install).
        joined = "\n".join(recipe.commands)
        assert "astral.sh/uv/install.sh" in joined
        assert "uv sync --extra dev" in recipe.commands
        assert "3.12" in joined  # python version note
        # Test runner explicitly NOT in commands
        assert not any("pytest" in c for c in recipe.commands)

    def test_finds_job_by_dictionary_key_when_name_field_absent(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              build-and-test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: uv sync
                  - run: pytest
        """)
        # failing_job_name matches the dict key, not a `name:` field
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="build-and-test"
        )
        assert recipe is not None
        assert recipe.job_key == "build-and-test"
        assert "uv sync" in recipe.commands


class TestPytestWithPip:
    """testbed-style workflow: setup-python + pip install + pytest."""

    def test_renders_install_and_skips_test_runner(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: actions/setup-python@v5
                    with:
                      python-version: '3.11'
                  - run: pip install -e .[dev]
                  - run: pytest --cov=src/calc --cov-fail-under=80
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        assert "pip install -e .[dev]" in recipe.commands
        # v1.7.1.1: test runner step (`pytest ...`) is excluded from
        # setup commands. SRE verify runs the failing_command separately.
        assert not any("pytest" in c for c in recipe.commands)


class TestPreCommitOnly:
    """Lint workflow: just pre-commit/action."""

    def test_renders_pre_commit_install_and_run(self, workspace):
        _write_workflow(workspace, "lint.yml", """\
            on: push
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: actions/setup-python@v5
                    with:
                      python-version: '3.12'
                  - uses: pre-commit/action@v3.0.1
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="lint"
        )
        assert recipe is not None
        assert "pip install pre-commit" in recipe.commands
        assert "pre-commit run --all-files" in recipe.commands

    def test_pre_commit_extra_args(self, workspace):
        _write_workflow(workspace, "lint.yml", """\
            on: push
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - uses: pre-commit/action@v3.0.1
                    with:
                      extra_args: '--show-diff-on-failure'
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="lint"
        )
        assert recipe is not None
        assert any("--show-diff-on-failure" in c for c in recipe.commands)


# ─── Bail-out cases — should return None ─────────────────────────────────────


class TestBailsToTier1:
    def test_matrix_template_literal_in_run_step_bails(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                strategy:
                  matrix:
                    python: ['3.10', '3.11', '3.12']
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: pytest --python ${{ matrix.python }}
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        # Unresolved template literal — bail to Tier 1
        assert recipe is None

    def test_unsupported_uses_action_bails(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: my-org/custom-installer@v1
                    with:
                      repo: foo
                  - run: pytest
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is None

    def test_codecov_uses_action_does_not_bail(self, workspace):
        """Some unsupported actions are known-safe-to-skip — they don't
        affect env setup, just upload telemetry. Recipe should still
        succeed in their presence.
        """
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: pip install -e .
                  - run: pytest
                  - uses: codecov/codecov-action@v3
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        assert "pip install -e ." in recipe.commands
        assert any("codecov" in s for s in recipe.unsupported_steps)

    def test_no_matching_job_returns_none(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: make build
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is None

    def test_no_workflow_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipe = extract_recipe(
                workspace_path=Path(tmp), failing_job_name="test"
            )
            assert recipe is None

    def test_malformed_yaml_returns_none(self, workspace):
        _write_workflow(workspace, "broken.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: pytest
                this_is_invalid_yaml: [
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is None


# ─── Run-step list form ──────────────────────────────────────────────────────


class TestTestRunnerFilter:
    """v1.7.1.1: test-runner steps are excluded from Tier 0's setup
    commands so SRE setup doesn't accidentally execute the failing test."""

    def test_ruff_check_excluded(self, workspace):
        _write_workflow(workspace, "lint.yml", """\
            on: push
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install ruff
                  - run: ruff check .
        """)
        recipe = extract_recipe(workspace_path=workspace, failing_job_name="lint")
        assert recipe is not None
        assert "pip install ruff" in recipe.commands
        assert not any("ruff check" in c for c in recipe.commands)

    def test_pytest_excluded_but_install_kept(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install -e .[dev]
                  - run: pytest tests/
        """)
        recipe = extract_recipe(workspace_path=workspace, failing_job_name="test")
        assert recipe is not None
        assert "pip install -e .[dev]" in recipe.commands
        assert not any("pytest" in c for c in recipe.commands)

    def test_mypy_excluded(self, workspace):
        _write_workflow(workspace, "type.yml", """\
            on: push
            jobs:
              type-check:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install mypy
                  - run: mypy src/
        """)
        recipe = extract_recipe(workspace_path=workspace, failing_job_name="type-check")
        assert recipe is not None
        assert "pip install mypy" in recipe.commands
        assert not any(c.startswith("mypy") for c in recipe.commands)

    def test_go_test_excluded(self, workspace):
        _write_workflow(workspace, "go.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: go mod download
                  - run: go test ./...
        """)
        recipe = extract_recipe(workspace_path=workspace, failing_job_name="test")
        assert recipe is not None
        assert "go mod download" in recipe.commands
        assert not any(c.startswith("go test") for c in recipe.commands)


class TestRunStepListForm:
    def test_run_as_list_is_joined(self, workspace):
        # YAML allows `run:` to be a literal block (`|`), folded (`>`), or list
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      pip install -e .
                      pip install pytest
                  - run: pytest
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        # Multi-line script gets preserved
        assert any("pip install -e ." in c for c in recipe.commands)
        assert any("pip install pytest" in c for c in recipe.commands)


# ─── Integration: multi-workflow repo ────────────────────────────────────────


class TestMultiWorkflowRepo:
    def test_picks_workflow_with_matching_job(self, workspace):
        # v1.7.1.1: workflow with ONLY a test runner step yields a recipe
        # with only the setup-derived commands (here: just the install
        # for the matching job).
        _write_workflow(workspace, "lint.yml", """\
            on: push
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install ruff
                  - run: ruff check .
        """)
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install pytest
                  - run: pytest
        """)
        # Failing job is "test" — extractor should pick test.yml's install
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        assert "pip install pytest" in recipe.commands
        # Test runner filtered out (v1.7.1.1)
        assert not any("pytest" == c for c in recipe.commands)
        # Other-workflow content not present
        assert not any("ruff" in c for c in recipe.commands)

    def test_picks_lint_workflow_when_lint_job_fails(self, workspace):
        _write_workflow(workspace, "lint.yml", """\
            on: push
            jobs:
              lint:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install ruff
                  - run: ruff check .
        """)
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install pytest
                  - run: pytest
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="lint"
        )
        assert recipe is not None
        assert "pip install ruff" in recipe.commands
        # Test runner filtered (v1.7.1.1)
        assert not any("ruff check" in c for c in recipe.commands)


# ─── Recipe shape sanity ─────────────────────────────────────────────────────


class TestRecipeShape:
    def test_recipe_carries_metadata(self, workspace):
        # v1.7.1.1: workflow needs at least one non-test step to yield
        # a recipe (otherwise the filter strips everything → None).
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                name: My Test Suite
                runs-on: ubuntu-22.04
                steps:
                  - run: pip install pytest
                  - run: pytest
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        assert recipe.workflow_file.endswith(".github/workflows/test.yml")
        assert recipe.job_key == "test"
        assert recipe.job_name == "My Test Suite"
        assert recipe.runs_on == "ubuntu-22.04"

    def test_recipe_is_immutable(self, workspace):
        _write_workflow(workspace, "test.yml", """\
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: pip install pytest
                  - run: pytest
        """)
        recipe = extract_recipe(
            workspace_path=workspace, failing_job_name="test"
        )
        assert recipe is not None
        # ExtractedRecipe is frozen dataclass — should raise on assign
        with pytest.raises(Exception):
            recipe.commands = []  # type: ignore[misc]
