"""v1.7.1 Tier 1 — lockfile fingerprint detection.

When Tier 0 (workflow YAML) doesn't yield a recipe (no workflow file,
unsupported step, template literal we can't expand), we fall through to
file-presence detection. Per docs/v171-provisioning-tiers.md research,
this catches another ~10% of repos before resorting to the agentic
Tier 2.

Detection priority (first match wins, ranked by reliability):

  1. uv.lock              → uv sync --frozen
  2. poetry.lock + pyproject  → poetry install
  3. Pipfile.lock         → pipenv sync
  4. pixi.toml + pixi.lock → pixi install
  5. pyproject.toml [project] + [project.optional-dependencies.dev]
                          → pip install -e .[dev]
  6. pyproject.toml [project] only
                          → pip install -e .
  7. requirements-dev.txt → pip install -r requirements-dev.txt
  8. requirements.txt     → pip install -r requirements.txt
  9. setup.py (legacy)    → pip install -e .

Returns None when none match — caller falls through to Tier 2 (agentic).

Design constraints:
  - Pure file-presence + tomllib parsing; no subprocess
  - Stable: same files → same recipe
  - Fast: no I/O outside reading 1-2 small files
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DetectedRecipe:
    """A recipe derived from lockfile/manifest fingerprinting.

    `commands` are executed in order; `detected_via` names the file or
    rule that triggered detection (for debugging + cache key signal).
    """

    detected_via: str           # e.g. "uv.lock", "pyproject.toml:[project.deps]"
    commands: list[str] = field(default_factory=list)


# ─── Detection rules in priority order ────────────────────────────────────────
#
# Each rule is a (predicate, command-builder) pair. First predicate that
# returns truthy "claims" the workspace. Build order matters — earlier
# rules win.


def _has_uv_lock(workspace: Path) -> bool:
    return (workspace / "uv.lock").is_file()


def _has_poetry_lock(workspace: Path) -> bool:
    if not (workspace / "poetry.lock").is_file():
        return False
    # Confirm pyproject has a [tool.poetry] section to avoid false match
    pyp = workspace / "pyproject.toml"
    if not pyp.is_file():
        return True  # poetry.lock alone is signal enough
    try:
        data = tomllib.loads(pyp.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return True
    return "poetry" in (data.get("tool") or {}) or _is_poetry_pyproject(data)


def _is_poetry_pyproject(data: dict) -> bool:
    return "poetry" in (data.get("tool") or {})


def _has_pipfile_lock(workspace: Path) -> bool:
    return (workspace / "Pipfile.lock").is_file()


def _has_pixi(workspace: Path) -> bool:
    return (
        (workspace / "pixi.toml").is_file()
        and (workspace / "pixi.lock").is_file()
    )


def _has_pyproject_with_dev_extras(workspace: Path) -> bool:
    pyp = workspace / "pyproject.toml"
    if not pyp.is_file():
        return False
    try:
        data = tomllib.loads(pyp.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return False
    project = data.get("project")
    if not isinstance(project, dict):
        return False
    extras = project.get("optional-dependencies")
    if not isinstance(extras, dict):
        return False
    return "dev" in extras or "test" in extras


def _has_pyproject_minimal(workspace: Path) -> bool:
    pyp = workspace / "pyproject.toml"
    if not pyp.is_file():
        return False
    try:
        data = tomllib.loads(pyp.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return False
    return "project" in data


def _has_requirements_dev(workspace: Path) -> bool:
    return (workspace / "requirements-dev.txt").is_file()


def _has_requirements(workspace: Path) -> bool:
    return (workspace / "requirements.txt").is_file()


def _has_setup_py(workspace: Path) -> bool:
    return (workspace / "setup.py").is_file()


def _pyproject_dev_extra_name(workspace: Path) -> str:
    """Return 'dev' or 'test' or '' depending on which extras the pyproject
    declares. 'dev' takes priority. Used to construct the right install
    command for the dev-extras detection rule.
    """
    pyp = workspace / "pyproject.toml"
    try:
        data = tomllib.loads(pyp.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return ""
    project = data.get("project") or {}
    extras = project.get("optional-dependencies") or {}
    if "dev" in extras:
        return "dev"
    if "test" in extras:
        return "test"
    return ""


# Detection rules in priority order. Each tuple is:
#   (rule_name, predicate, command_builder)
# command_builder takes the workspace path and returns list[str] commands.


def _build_uv_sync(workspace: Path) -> list[str]:
    return ["uv sync --frozen"]


def _build_poetry_install(workspace: Path) -> list[str]:
    return ["poetry install"]


def _build_pipenv_sync(workspace: Path) -> list[str]:
    return ["pipenv sync --dev"]


def _build_pixi_install(workspace: Path) -> list[str]:
    return ["pixi install"]


def _build_pip_install_with_extras(workspace: Path) -> list[str]:
    extra = _pyproject_dev_extra_name(workspace)
    if extra:
        return [f"pip install -e .[{extra}]"]
    return ["pip install -e ."]


def _build_pip_install_minimal(workspace: Path) -> list[str]:
    return ["pip install -e ."]


def _build_pip_requirements_dev(workspace: Path) -> list[str]:
    cmds = ["pip install -r requirements-dev.txt"]
    if _has_requirements(workspace):
        cmds.insert(0, "pip install -r requirements.txt")
    return cmds


def _build_pip_requirements(workspace: Path) -> list[str]:
    return ["pip install -r requirements.txt"]


def _build_setup_py_install(workspace: Path) -> list[str]:
    return ["pip install -e ."]


_DETECTION_RULES: list[tuple[str, callable, callable]] = [
    ("uv.lock", _has_uv_lock, _build_uv_sync),
    ("poetry.lock", _has_poetry_lock, _build_poetry_install),
    ("Pipfile.lock", _has_pipfile_lock, _build_pipenv_sync),
    ("pixi.toml+pixi.lock", _has_pixi, _build_pixi_install),
    (
        "pyproject.toml:[project.optional-dependencies]",
        _has_pyproject_with_dev_extras,
        _build_pip_install_with_extras,
    ),
    (
        "pyproject.toml:[project]",
        _has_pyproject_minimal,
        _build_pip_install_minimal,
    ),
    ("requirements-dev.txt", _has_requirements_dev, _build_pip_requirements_dev),
    ("requirements.txt", _has_requirements, _build_pip_requirements),
    ("setup.py", _has_setup_py, _build_setup_py_install),
]


def detect_recipe(*, workspace_path: str | Path) -> DetectedRecipe | None:
    """Top-level entry point. Apply detection rules in priority order;
    return the first match's recipe, or None if no rule matches.
    """
    workspace = Path(workspace_path)
    if not workspace.is_dir():
        return None

    for rule_name, predicate, builder in _DETECTION_RULES:
        try:
            if not predicate(workspace):
                continue
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "v171.lockfile.predicate_error",
                rule=rule_name,
                error=str(exc),
            )
            continue
        commands = builder(workspace)
        log.info(
            "v171.lockfile.detected",
            rule=rule_name,
            n_commands=len(commands),
        )
        return DetectedRecipe(detected_via=rule_name, commands=commands)

    return None


__all__ = [
    "DetectedRecipe",
    "detect_recipe",
]
