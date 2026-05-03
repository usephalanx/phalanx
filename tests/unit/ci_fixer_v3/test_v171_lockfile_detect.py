"""Tier-1 tests for v1.7.1 lockfile fingerprint detector.

Each test sets up a tempdir with a specific file fingerprint and asserts
the detector picks the right install command. Priority-order tests pin
the precedence (uv.lock wins over poetry.lock wins over requirements.txt).

All file-presence checks; no network, no subprocess.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from phalanx.agents._v171_lockfile_detect import (
    DetectedRecipe,
    detect_recipe,
)


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _w(workspace: Path, name: str, content: str = "") -> None:
    """Write a file to the workspace."""
    (workspace / name).write_text(content)


# ─── Single-rule detection (each rule in isolation) ──────────────────────────


class TestSingleRuleDetection:
    def test_uv_lock_renders_uv_sync(self, workspace):
        _w(workspace, "uv.lock", "# locked\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "uv.lock"
        assert recipe.commands == ["uv sync --frozen"]

    def test_poetry_lock_renders_poetry_install(self, workspace):
        _w(workspace, "poetry.lock", "# poetry locked\n")
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [tool.poetry]
            name = "foo"
            version = "0.1.0"
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "poetry.lock"
        assert recipe.commands == ["poetry install"]

    def test_pipfile_lock_renders_pipenv_sync(self, workspace):
        _w(workspace, "Pipfile.lock", '{"_meta":{}}\n')
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "Pipfile.lock"
        assert recipe.commands == ["pipenv sync --dev"]

    def test_pixi_renders_pixi_install(self, workspace):
        _w(workspace, "pixi.toml", "[project]\nname='x'\n")
        _w(workspace, "pixi.lock", "version: 5\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "pixi.toml+pixi.lock"
        assert recipe.commands == ["pixi install"]

    def test_pyproject_with_dev_extras_renders_install_with_dev(self, workspace):
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [project]
            name = "foo"
            version = "0.1.0"

            [project.optional-dependencies]
            dev = ["pytest>=8"]
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert "[project.optional-dependencies]" in recipe.detected_via
        assert recipe.commands == ["pip install -e .[dev]"]

    def test_pyproject_with_test_extras_renders_install_with_test(self, workspace):
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [project]
            name = "foo"
            version = "0.1.0"

            [project.optional-dependencies]
            test = ["pytest"]
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.commands == ["pip install -e .[test]"]

    def test_pyproject_minimal_renders_plain_install(self, workspace):
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [project]
            name = "foo"
            version = "0.1.0"
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.commands == ["pip install -e ."]

    def test_requirements_dev_with_requirements_renders_both(self, workspace):
        _w(workspace, "requirements.txt", "requests\n")
        _w(workspace, "requirements-dev.txt", "pytest\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "requirements-dev.txt"
        # Both get installed, in dependency order
        assert recipe.commands == [
            "pip install -r requirements.txt",
            "pip install -r requirements-dev.txt",
        ]

    def test_requirements_only_renders_pip_r(self, workspace):
        _w(workspace, "requirements.txt", "requests\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "requirements.txt"
        assert recipe.commands == ["pip install -r requirements.txt"]

    def test_setup_py_only_renders_install(self, workspace):
        _w(workspace, "setup.py", "from setuptools import setup\nsetup(name='x')\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "setup.py"
        assert recipe.commands == ["pip install -e ."]

    def test_no_recognized_files_returns_none(self, workspace):
        _w(workspace, "README.md", "# hello\n")
        _w(workspace, "Makefile", "test:\n\techo hi\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is None


# ─── Priority-order tests ────────────────────────────────────────────────────


class TestPriorityOrder:
    def test_uv_lock_beats_poetry_lock(self, workspace):
        _w(workspace, "uv.lock", "")
        _w(workspace, "poetry.lock", "")
        _w(workspace, "pyproject.toml", "[tool.poetry]\nname='x'\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "uv.lock"

    def test_uv_lock_beats_requirements_txt(self, workspace):
        _w(workspace, "uv.lock", "")
        _w(workspace, "requirements.txt", "x\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "uv.lock"

    def test_poetry_lock_beats_pyproject_extras(self, workspace):
        _w(workspace, "poetry.lock", "")
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [tool.poetry]
            name = "x"
            version = "0.1"

            [project]
            name = "x"
            version = "0.1"

            [project.optional-dependencies]
            dev = ["pytest"]
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        # Poetry has higher priority than naive [project] detection
        assert recipe.detected_via == "poetry.lock"

    def test_pyproject_dev_beats_minimal_pyproject(self, workspace):
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [project]
            name = "x"
            version = "0.1"

            [project.optional-dependencies]
            dev = ["pytest"]
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        # The "with dev extras" rule wins, not "minimal pyproject"
        assert "[project.optional-dependencies]" in recipe.detected_via
        assert recipe.commands == ["pip install -e .[dev]"]

    def test_requirements_dev_beats_requirements_alone(self, workspace):
        _w(workspace, "requirements.txt", "x\n")
        _w(workspace, "requirements-dev.txt", "pytest\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        assert recipe.detected_via == "requirements-dev.txt"


# ─── Robustness ──────────────────────────────────────────────────────────────


class TestRobustness:
    def test_malformed_pyproject_falls_back(self, workspace):
        # Even a TOML parse error shouldn't crash — detector continues
        # to next rule, lands on something else (or returns None)
        _w(workspace, "pyproject.toml", "this is not valid TOML [[")
        _w(workspace, "requirements.txt", "x\n")
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is not None
        # Should fall through to requirements.txt
        assert recipe.detected_via == "requirements.txt"

    def test_non_directory_workspace_returns_none(self):
        recipe = detect_recipe(workspace_path="/nonexistent/path/xyz")
        assert recipe is None

    def test_pyproject_without_project_section_returns_none(self, workspace):
        # A pyproject with only [build-system] and no [project] is real —
        # some pre-PEP-621 packages. Detector should not match.
        _w(workspace, "pyproject.toml", textwrap.dedent("""\
            [build-system]
            requires = ["setuptools"]
        """))
        recipe = detect_recipe(workspace_path=workspace)
        assert recipe is None
