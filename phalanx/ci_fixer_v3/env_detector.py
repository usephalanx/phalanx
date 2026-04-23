"""Environment detection for CI-Fixer v3 on-the-fly sandbox provisioning.

Given a cloned workspace, produce an EnvSpec describing everything the
sandbox needs to mimic the repo's actual CI:
  - Which base Docker image (matching the repo's Python/Node/Java version)
  - Which system packages (apt) to install
  - Which pip/npm/mvn commands to run
  - Which tool versions the CI pins (ruff, pytest, mypy, ...)

This module is the ANTIDOTE to the pre-warmed sandbox staleness problem
that blocked the humanize canary (pinned ruff 0.4.4 couldn't parse a
modern pyproject.toml). We read the repo, not our baked image.

Design goals:
  - Pure functions. No Docker, no DB, no network. Deterministic given a
    workspace path. Trivially unit-testable.
  - Fail soft: unknown files / malformed inputs are skipped with a log
    note, not raised. The SRE agent sees a partial EnvSpec and can
    decide whether it's sufficient.
  - Phase 1 scope: Python. Other stacks produce a minimal EnvSpec that
    defers to the existing pool image (graceful degradation so v3 can
    run on any stack even before we've implemented full detection).
"""

from __future__ import annotations

import re
import tomllib  # Python 3.11+
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class EnvSpec:
    """Declarative sandbox provisioning recipe for one CI-fix run.

    The SRE agent's "setup mode" consumes this and hands a ready container
    to downstream agents. Fields are intentionally explicit — no magic
    in the provisioner; everything it does is derivable from the repo.
    """

    stack: str
    """python | node | java | csharp | go | rust | unknown"""

    base_image: str
    """Docker image the sandbox starts FROM. Minimal — we install on top."""

    workspace_path: str
    """Absolute path to the cloned repo on the host."""

    python_version: str | None = None
    """Major.minor from requires-python (e.g. '3.12'), if detected."""

    system_deps: list[str] = field(default_factory=list)
    """apt packages to install before pip/npm/mvn. E.g. ['gettext', 'git']."""

    install_commands: list[str] = field(default_factory=list)
    """Shell commands run in the container, in order, during provisioning.

    Examples:
      'pip install --upgrade pip'
      'pip install -e ".[tests]"'
      'pip install -r requirements-dev.txt'

    The provisioner runs these with cwd=/workspace after the workspace is
    copied into the container."""

    tool_versions: dict[str, str] = field(default_factory=dict)
    """Detected pinned versions (for diagnostics + future cache keys).
    E.g. {'ruff': '0.14.0', 'pytest': '8.2.0'}. Not authoritative —
    install_commands still govern what's actually installed."""

    detected_from: list[str] = field(default_factory=list)
    """Repo-relative paths the detector read. Useful for debugging and
    for the SRE agent to explain its provisioning choices."""

    notes: list[str] = field(default_factory=list)
    """Human-readable observations (e.g., 'found legacy setup.py — treating
    as modern package'). Surfaced in SRE's Task.output."""

    def to_json(self) -> dict:
        """Serializable shape for storage in Task.output."""
        return {
            "stack": self.stack,
            "base_image": self.base_image,
            "workspace_path": self.workspace_path,
            "python_version": self.python_version,
            "system_deps": list(self.system_deps),
            "install_commands": list(self.install_commands),
            "tool_versions": dict(self.tool_versions),
            "detected_from": list(self.detected_from),
            "notes": list(self.notes),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def detect_env(workspace_path: str | Path) -> EnvSpec:
    """Walk a cloned workspace and return an EnvSpec.

    Never raises on malformed inputs — logs and degrades to safer defaults.
    Callers that need strict validation should inspect the returned
    EnvSpec for empty install_commands / stack=='unknown' / etc.
    """
    root = Path(workspace_path).resolve()
    if not root.is_dir():
        log.warning("env_detector.workspace_missing", path=str(root))
        return EnvSpec(
            stack="unknown",
            base_image="ubuntu:22.04",
            workspace_path=str(root),
            notes=[f"workspace path {root} does not exist"],
        )

    stack = _detect_stack(root)
    if stack == "python":
        spec = _detect_python_env(root)
    else:
        # Phase 1: other stacks get a minimal pass-through EnvSpec. The SRE
        # agent will see stack + base_image and can either take the
        # conservative path (use pool image) or ask for more detection.
        spec = EnvSpec(
            stack=stack,
            base_image=_default_base_image(stack),
            workspace_path=str(root),
            notes=[f"Phase 1: detailed env detection not yet implemented for stack={stack!r}"],
        )

    # Workflow YAML pass runs for every stack — apt deps + test commands are
    # cross-language signals.
    _augment_from_workflows(root, spec)
    return spec


# ─────────────────────────────────────────────────────────────────────────────
# Stack detection
# ─────────────────────────────────────────────────────────────────────────────


_STACK_MARKERS: dict[str, tuple[str, ...]] = {
    # Order matters: more specific first. pyproject.toml alone = python.
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    "node": ("package.json",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "csharp": ("*.csproj", "*.sln", "global.json"),
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
}


def _detect_stack(root: Path) -> str:
    """First-match wins on marker files. Returns 'unknown' if nothing hit."""
    for stack, markers in _STACK_MARKERS.items():
        for m in markers:
            if "*" in m:
                if any(root.rglob(m)):
                    return stack
            elif (root / m).exists():
                return stack
    return "unknown"


def _default_base_image(stack: str) -> str:
    """Minimal image per stack — NOT the phalanx-sandbox-*:latest pool image."""
    return {
        "python": "python:3.12-slim",
        "node": "node:20-slim",
        "java": "maven:3.9-eclipse-temurin-21",
        "csharp": "mcr.microsoft.com/dotnet/sdk:8.0",
        "go": "golang:1.22-alpine",
        "rust": "rust:1.77-slim",
        "unknown": "ubuntu:22.04",
    }.get(stack, "ubuntu:22.04")


# ─────────────────────────────────────────────────────────────────────────────
# Python detection (Phase 1 scope)
# ─────────────────────────────────────────────────────────────────────────────


def _detect_python_env(root: Path) -> EnvSpec:
    spec = EnvSpec(
        stack="python",
        base_image="python:3.12-slim",  # overridden below if requires-python pins older
        workspace_path=str(root),
    )

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        _read_pyproject_into_spec(pyproject, spec)

    # Legacy repos: setup.py or requirements.txt without pyproject. Fall back.
    if not spec.install_commands:
        if (root / "requirements.txt").exists():
            spec.install_commands.append("pip install --upgrade pip")
            spec.install_commands.append("pip install -r requirements.txt")
            spec.detected_from.append("requirements.txt")
        elif (root / "setup.py").exists():
            spec.install_commands.append("pip install --upgrade pip")
            spec.install_commands.append("pip install -e .[dev] || pip install -e .")
            spec.detected_from.append("setup.py")
            spec.notes.append(
                "legacy setup.py detected; attempted install with [dev] extras "
                "(falls back to bare -e . if extras don't exist)"
            )

    # Always ensure pip is current — too many modern packages require it.
    if not any("pip install --upgrade pip" in c for c in spec.install_commands):
        spec.install_commands.insert(0, "pip install --upgrade pip")

    return spec


def _read_pyproject_into_spec(pyproject: Path, spec: EnvSpec) -> None:
    try:
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        spec.notes.append(f"pyproject.toml unreadable: {exc}")
        log.warning("env_detector.pyproject_read_failed", error=str(exc))
        return
    spec.detected_from.append("pyproject.toml")

    project = data.get("project") or {}

    # Python version: take the lower bound from requires-python
    req_py = project.get("requires-python")
    if isinstance(req_py, str):
        spec.python_version = _extract_python_minor_version(req_py)
        if spec.python_version:
            spec.base_image = f"python:{spec.python_version}-slim"

    # Install command: prefer editable + first non-empty optional-dep group.
    # Modern Python projects usually have a 'dev' / 'tests' / 'test' group.
    opt_deps = project.get("optional-dependencies") or {}
    preferred_groups = ("dev", "tests", "test", "all")
    chosen_group: str | None = None
    for g in preferred_groups:
        if g in opt_deps and opt_deps[g]:
            chosen_group = g
            break
    if chosen_group is None and opt_deps:
        chosen_group = next(iter(opt_deps.keys()))

    if chosen_group is not None:
        spec.install_commands.append(f'pip install -e ".[{chosen_group}]"')
        spec.notes.append(f"using optional-dependencies group {chosen_group!r}")
    else:
        # No extras declared — plain editable install picks up [project.dependencies].
        spec.install_commands.append("pip install -e .")

    # Tool versions: poke at [tool.*] blocks for common lint/test pins.
    tool = data.get("tool") or {}
    # Hatch-style build system often implies hatch-vcs for versioning; not a pin.
    # Look at ruff/mypy/pytest/black in a few common config locations.
    ruff_block = tool.get("ruff") or {}
    if isinstance(ruff_block, dict):
        # ruff doesn't pin its own version in pyproject — it's installed separately.
        # But if the config uses newer fields (lint.select), the running ruff must
        # be >= 0.5. Record as "min_version".
        if "lint" in ruff_block or ruff_block.get("extend-select"):
            spec.tool_versions["ruff"] = ">=0.5"
            spec.notes.append(
                "pyproject.toml uses ruff lint.* config; requires ruff >= 0.5"
            )

    pytest_block = (tool.get("pytest") or {}).get("ini_options") or {}
    if pytest_block:
        spec.tool_versions.setdefault("pytest", "*")

    mypy_block = tool.get("mypy") or {}
    if mypy_block:
        spec.tool_versions.setdefault("mypy", "*")


def _extract_python_minor_version(req_py: str) -> str | None:
    """Turn '>=3.10' / '==3.11.*' / '>=3.10,<3.13' into '3.12'-ish version string.

    Strategy: prefer the lower bound. If it's `>=3.10`, return `3.12` (latest
    stable that satisfies). If it's pinned to `3.11`, return `3.11`.

    This is deliberately simple — a real repo's CI matrix may run multiple
    versions; we pick one. Phase 2 could honor the matrix by provisioning
    multiple sandboxes.
    """
    match = re.search(r"(\d+)\.(\d+)", req_py)
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2))
    # If the lower bound is lower than 3.12 but the spec uses `>=`, we can
    # run on 3.12 (forward compatible). If it's `==` / `~=`, respect the pin.
    is_upper = any(op in req_py for op in (">=", ">"))
    if is_upper and minor < 12:
        return "3.12"
    return f"{major}.{minor}"


# ─────────────────────────────────────────────────────────────────────────────
# Workflow YAML augmentation (cross-stack)
# ─────────────────────────────────────────────────────────────────────────────


_APT_INSTALL_RE = re.compile(
    r"(?:sudo\s+)?apt(?:-get)?\s+install\s+(?:-y\s+|--yes\s+)?([\w\-\s]+)",
    re.IGNORECASE,
)


def _augment_from_workflows(root: Path, spec: EnvSpec) -> None:
    """Parse `.github/workflows/*.yml` for apt installs and pinned tool versions.

    Intentionally lightweight: we DON'T try to fully execute workflow YAML
    (matrices, expressions, conditionals). We just pattern-match common
    lines — the goal is to catch `apt install gettext` so our sandbox has
    what the repo assumed, not to be a full GHA runner.
    """
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return

    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        try:
            text = wf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        spec.detected_from.append(f".github/workflows/{wf.name}")
        for pkgs in _APT_INSTALL_RE.findall(text):
            for pkg in pkgs.split():
                pkg = pkg.strip()
                if pkg and pkg not in spec.system_deps and pkg.replace("-", "").isalnum():
                    spec.system_deps.append(pkg)
