"""
Verification Profiles — declarative build/check config per tech stack.

Each profile describes how to install, type-check, build, and lint a project
for a given technology stack. The VerifierAgent and IntegrationWiringAgent
use these profiles instead of hardcoded if/else blocks, making it trivial to
add support for a new stack: one entry in PROFILES, no agent code changes.

Usage:
    from phalanx.agents.verification_profiles import get_profile, detect_tech_stack

    tech_stack = detect_tech_stack(work_dir, app_type="web")
    profile = get_profile(tech_stack)
    errors = run_profile_checks(profile, work_dir)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003

import structlog

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# VerificationProfile dataclass
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerificationProfile:
    """
    Immutable verification config for a specific technology stack.

    Fields:
        tech_stack:         canonical key e.g. "nextjs", "fastapi"
        app_type:           coarse classifier: web | api | mobile | cli
        entry_points:       relative paths expected to exist after wiring
        detection_files:    files whose presence (in workspace root) identifies this stack
        install_cmd:        dependency install command, or empty list if not needed
        build_cmd:          compile / build command
        typecheck_cmd:      static type-check command (optional)
        lint_cmd:           lint command (optional)
        install_timeout:    seconds before install is killed
        build_timeout:      seconds before build is killed
        integration_pattern: wiring strategy key used by IntegrationWiringAgent
    """

    tech_stack: str
    app_type: str
    entry_points: list[str]
    detection_files: list[str]
    install_cmd: list[str]
    build_cmd: list[str]
    typecheck_cmd: list[str]
    lint_cmd: list[str]
    install_timeout: int
    build_timeout: int
    integration_pattern: str


# ─────────────────────────────────────────────────────────────────────────────
# Profile registry
# ─────────────────────────────────────────────────────────────────────────────

PROFILES: dict[str, VerificationProfile] = {
    # ── Web ──────────────────────────────────────────────────────────────────
    "nextjs": VerificationProfile(
        tech_stack="nextjs",
        app_type="web",
        entry_points=["app/page.tsx", "app/page.jsx", "pages/index.tsx", "pages/index.jsx"],
        detection_files=["next.config.js", "next.config.ts", "next.config.mjs"],
        install_cmd=["npm", "install", "--legacy-peer-deps"],
        build_cmd=["npm", "run", "build"],
        typecheck_cmd=["npx", "tsc", "--noEmit"],
        lint_cmd=[],
        install_timeout=120,
        build_timeout=180,
        integration_pattern="nextjs-app-router",
    ),
    "vite": VerificationProfile(
        tech_stack="vite",
        app_type="web",
        entry_points=["src/App.tsx", "src/App.jsx", "src/main.tsx", "src/main.jsx"],
        detection_files=["vite.config.ts", "vite.config.js"],
        install_cmd=["npm", "install", "--legacy-peer-deps"],
        build_cmd=["npm", "run", "build"],
        typecheck_cmd=["npx", "tsc", "--noEmit"],
        lint_cmd=[],
        install_timeout=120,
        build_timeout=120,
        integration_pattern="vite-app",
    ),
    "sveltekit": VerificationProfile(
        tech_stack="sveltekit",
        app_type="web",
        entry_points=["src/routes/+page.svelte"],
        detection_files=["svelte.config.js", "svelte.config.ts"],
        install_cmd=["npm", "install"],
        build_cmd=["npm", "run", "build"],
        typecheck_cmd=["npx", "svelte-check"],
        lint_cmd=[],
        install_timeout=120,
        build_timeout=120,
        integration_pattern="sveltekit-routes",
    ),
    "generic_web": VerificationProfile(
        tech_stack="generic_web",
        app_type="web",
        entry_points=["src/index.tsx", "src/index.jsx", "src/App.tsx", "index.html"],
        detection_files=["package.json"],
        install_cmd=["npm", "install", "--legacy-peer-deps"],
        build_cmd=["npm", "run", "build"],
        typecheck_cmd=[],
        lint_cmd=[],
        install_timeout=120,
        build_timeout=180,
        integration_pattern="generic-web",
    ),
    # ── API ───────────────────────────────────────────────────────────────────
    "fastapi": VerificationProfile(
        tech_stack="fastapi",
        app_type="api",
        entry_points=["main.py", "app/main.py"],
        detection_files=["main.py", "app/main.py"],  # plus fastapi import check
        install_cmd=["pip", "install", "-r", "requirements.txt", "--quiet"],
        build_cmd=["python", "-m", "py_compile"],  # placeholder — real check in _compile_all_py
        typecheck_cmd=[
            "python",
            "-m",
            "mypy",
            ".",
            "--ignore-missing-imports",
            "--no-error-summary",
        ],
        lint_cmd=[],
        install_timeout=60,
        build_timeout=30,
        integration_pattern="fastapi-router",
    ),
    "django": VerificationProfile(
        tech_stack="django",
        app_type="api",
        entry_points=["manage.py"],
        detection_files=["manage.py"],
        install_cmd=["pip", "install", "-r", "requirements.txt", "--quiet"],
        build_cmd=["python", "manage.py", "check", "--no-color"],
        typecheck_cmd=[],
        lint_cmd=[],
        install_timeout=60,
        build_timeout=30,
        integration_pattern="django-urls",
    ),
    "express": VerificationProfile(
        tech_stack="express",
        app_type="api",
        entry_points=["src/index.ts", "src/app.ts", "index.js"],
        detection_files=["package.json"],  # plus express dep check
        install_cmd=["npm", "install"],
        build_cmd=["npx", "tsc", "--noEmit"],
        typecheck_cmd=[],
        lint_cmd=[],
        install_timeout=60,
        build_timeout=60,
        integration_pattern="express-router",
    ),
    "go": VerificationProfile(
        tech_stack="go",
        app_type="api",
        entry_points=["main.go", "cmd/main.go"],
        detection_files=["go.mod"],
        install_cmd=[],  # go modules handled by go build
        build_cmd=["go", "build", "./..."],
        typecheck_cmd=["go", "vet", "./..."],
        lint_cmd=[],
        install_timeout=0,
        build_timeout=60,
        integration_pattern="go-main",
    ),
    "generic_python": VerificationProfile(
        tech_stack="generic_python",
        app_type="api",
        entry_points=["main.py", "app.py", "run.py"],
        detection_files=["requirements.txt", "pyproject.toml"],
        install_cmd=[],
        build_cmd=["python", "-m", "py_compile"],
        typecheck_cmd=[],
        lint_cmd=[],
        install_timeout=0,
        build_timeout=30,
        integration_pattern="generic-python",
    ),
    # ── Mobile ────────────────────────────────────────────────────────────────
    "react_native": VerificationProfile(
        tech_stack="react_native",
        app_type="mobile",
        entry_points=["App.tsx", "App.js", "src/App.tsx"],
        detection_files=["app.json"],  # plus react-native dep check
        install_cmd=["npm", "install"],
        build_cmd=[],  # no headless build; tsc is the check
        typecheck_cmd=["npx", "tsc", "--noEmit"],
        lint_cmd=[],
        install_timeout=120,
        build_timeout=120,
        integration_pattern="rn-navigation",
    ),
    "expo": VerificationProfile(
        tech_stack="expo",
        app_type="mobile",
        entry_points=["App.tsx", "App.js", "app/(tabs)/index.tsx"],
        detection_files=["app.json", "expo.json"],
        install_cmd=["npm", "install"],
        build_cmd=["npx", "expo", "export", "--platform", "web", "--no-minify"],
        typecheck_cmd=["npx", "tsc", "--noEmit"],
        lint_cmd=[],
        install_timeout=120,
        build_timeout=180,
        integration_pattern="expo-tabs",
    ),
    "flutter": VerificationProfile(
        tech_stack="flutter",
        app_type="mobile",
        entry_points=["lib/main.dart"],
        detection_files=["pubspec.yaml"],
        install_cmd=["flutter", "pub", "get"],
        build_cmd=["flutter", "analyze"],
        typecheck_cmd=[],
        lint_cmd=[],
        install_timeout=60,
        build_timeout=120,
        integration_pattern="flutter-material",
    ),
    # ── CLI ───────────────────────────────────────────────────────────────────
    "click_cli": VerificationProfile(
        tech_stack="click_cli",
        app_type="cli",
        entry_points=["cli.py", "main.py", "src/cli.py"],
        detection_files=["pyproject.toml", "setup.py"],
        install_cmd=[],
        build_cmd=["python", "-m", "py_compile"],
        typecheck_cmd=[],
        lint_cmd=[],
        install_timeout=0,
        build_timeout=30,
        integration_pattern="python-click",
    ),
}

# Canonical fallbacks when detection fails
_FALLBACK_BY_APP_TYPE: dict[str, str] = {
    "web": "generic_web",
    "api": "generic_python",
    "cli": "generic_python",
    "mobile": "react_native",
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def get_profile(tech_stack: str) -> VerificationProfile:
    """Return profile for tech_stack, falling back to generic_web."""
    return PROFILES.get(tech_stack) or PROFILES["generic_web"]


def detect_tech_stack(work_dir: Path | None, app_type: str) -> str:
    """
    Detect the tech stack from filesystem heuristics.

    When work_dir is None (planning time — no workspace yet), uses app_type
    fallback mapping only.

    Returns a key guaranteed to exist in PROFILES.
    """
    if work_dir is None or not work_dir.exists():
        return _fallback_for(app_type)

    # ── Most specific checks first ────────────────────────────────────────────

    # Flutter
    if (work_dir / "pubspec.yaml").exists():
        return "flutter"

    # Expo (must check before react_native — expo has app.json too)
    if (work_dir / "app.json").exists() or (work_dir / "expo.json").exists():
        pkg = _read_pkg_deps(work_dir)
        if "expo" in pkg:
            return "expo"

    # React Native
    if (work_dir / "app.json").exists():
        pkg = _read_pkg_deps(work_dir)
        if "react-native" in pkg:
            return "react_native"

    # Go
    if (work_dir / "go.mod").exists():
        return "go"

    # SvelteKit
    for f in ["svelte.config.js", "svelte.config.ts"]:
        if (work_dir / f).exists():
            return "sveltekit"

    # Next.js
    for f in ["next.config.js", "next.config.ts", "next.config.mjs"]:
        if (work_dir / f).exists():
            return "nextjs"

    # Vite
    for f in ["vite.config.ts", "vite.config.js"]:
        if (work_dir / f).exists():
            return "vite"

    # Django
    if (work_dir / "manage.py").exists():
        return "django"

    # FastAPI — check for fastapi import in main.py
    for candidate in ["main.py", "app/main.py"]:
        p = work_dir / candidate
        if p.exists():
            try:
                content = p.read_text(errors="ignore")
                if "fastapi" in content.lower():
                    return "fastapi"
            except OSError:
                pass

    # Express (Node API — package.json with express dep)
    if (work_dir / "package.json").exists():
        pkg = _read_pkg_deps(work_dir)
        if "express" in pkg:
            return "express"
        # Generic web — has package.json but no specific framework
        return "generic_web"

    # Click CLI — pyproject.toml or setup.py with click import
    if (work_dir / "pyproject.toml").exists() or (work_dir / "setup.py").exists():
        for py_file in list(work_dir.glob("*.py")) + list(
            (work_dir / "src").glob("*.py") if (work_dir / "src").exists() else []
        ):
            try:
                if "click" in py_file.read_text(errors="ignore").lower():
                    return "click_cli"
            except OSError:
                pass

    # Python files without framework signals
    if list(work_dir.glob("*.py")):
        return "generic_python"

    # Ultimate fallback based on app_type
    return _fallback_for(app_type)


def merge_workspace(base: Path, builder_tasks: list) -> Path:
    """
    Merge all epic workspace directories into base/_merged/.

    Later epics win on file conflicts. Both IntegrationWiringAgent and
    VerifierAgent call this to get a single unified workspace.

    Args:
        base:          run workspace root (git_workspace / project_id / run_id)
        builder_tasks: list of Task ORM objects with branch_name set

    Returns:
        Path to the merged directory.
    """
    merged_dir = base / "_merged"
    if merged_dir.exists():
        shutil.rmtree(merged_dir)
    merged_dir.mkdir(parents=True)

    epic_dirs = sorted(
        {base / (t.branch_name.replace("/", "_") if t.branch_name else "") for t in builder_tasks}
    )

    for epic_dir in epic_dirs:
        if epic_dir.is_dir() and epic_dir != merged_dir:
            _copy_tree(epic_dir, merged_dir)

    log.debug("workspace.merged", merged_dir=str(merged_dir), epic_count=len(epic_dirs))
    return merged_dir


def run_profile_checks(profile: VerificationProfile, work_dir: Path) -> list[str]:
    """
    Run install → build → typecheck for the given profile.

    Returns list of error strings. Empty list = all checks passed.
    """
    errors: list[str] = []

    # Install
    if profile.install_cmd:
        ok, _, stderr = _run(profile.install_cmd, work_dir, profile.install_timeout)
        if not ok:
            errors.append(f"[install] {stderr.strip()[:200]}")
            return errors  # no point running build if install failed

    # Special case: py_compile means compile all .py files
    if profile.build_cmd == ["python", "-m", "py_compile"]:
        errors.extend(_compile_all_py(work_dir))
    elif profile.build_cmd:
        ok, stdout, stderr = _run(profile.build_cmd, work_dir, profile.build_timeout)
        if not ok:
            raw = (stdout + stderr).splitlines()
            errs = [ln for ln in raw if "error" in ln.lower()][:10]
            errors.extend(errs or [f"[build] command failed: {' '.join(profile.build_cmd)}"])

    # Typecheck (non-fatal contribution — collect but don't early-exit)
    if profile.typecheck_cmd and not errors:
        ok, stdout, stderr = _run(profile.typecheck_cmd, work_dir, profile.build_timeout)
        if not ok:
            raw = (stdout + stderr).splitlines()
            errs = [ln for ln in raw if "error" in ln.lower() or "Error" in ln][:5]
            errors.extend(errs or [f"[typecheck] {' '.join(profile.typecheck_cmd)} failed"])

    # Validate entry points exist
    ep_errors = _check_entry_points(profile, work_dir)
    errors.extend(ep_errors)

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _fallback_for(app_type: str) -> str:
    return _FALLBACK_BY_APP_TYPE.get(app_type, "generic_web")


def _read_pkg_deps(work_dir: Path) -> set[str]:
    """Return all dependency names from package.json (deps + devDeps)."""
    pkg = work_dir / "package.json"
    if not pkg.exists():
        return set()
    try:
        data = json.loads(pkg.read_text())
        return set(data.get("dependencies", {}).keys()) | set(
            data.get("devDependencies", {}).keys()
        )
    except (json.JSONDecodeError, OSError):
        return set()


def _check_entry_points(profile: VerificationProfile, work_dir: Path) -> list[str]:
    """Return errors for missing entry points. At least one must exist."""
    present = [ep for ep in profile.entry_points if (work_dir / ep).exists()]
    if profile.entry_points and not present:
        return [
            f"[entry_point] None of the expected entry points found: {profile.entry_points}. "
            f"Check that the integration wiring step completed successfully."
        ]
    return []


def _compile_all_py(work_dir: Path) -> list[str]:
    """py_compile every .py file (excluding .venv, __pycache__)."""
    errors: list[str] = []
    for py_file in sorted(work_dir.rglob("*.py")):
        if any(p in py_file.parts for p in (".venv", "__pycache__", "node_modules")):
            continue
        ok, _, stderr = _run(["python3", "-m", "py_compile", str(py_file)], work_dir, timeout=10)
        if not ok:
            errors.append(f"[compile] {py_file.name}: {stderr.strip()[:120]}")
    return errors[:10]


def _copy_tree(src: Path, dst: Path) -> None:
    """Recursively copy src into dst, skipping .git and __pycache__."""
    for item in src.rglob("*"):
        if item.is_file() and not any(p in item.parts for p in (".git", "__pycache__", ".venv")):
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _discover_react_components(components_dir: Path) -> list[dict]:
    """
    Scan a components directory for .tsx/.jsx files that look like React components.

    Returns list of {"name": "Hero", "relative_path": "Hero.tsx"} dicts.
    Skips layout/, ui/, lib/ subdirectories and non-component files.
    """
    components = []
    skip_dirs = {"layout", "ui", "__tests__", "test", "lib", "utils", "hooks", "context"}

    for tsx_file in sorted(components_dir.rglob("*.tsx")) + sorted(components_dir.rglob("*.jsx")):
        if any(part in skip_dirs for part in tsx_file.parts):
            continue
        if tsx_file.stem.startswith("__") or tsx_file.stem.lower() in ("index",):
            continue

        try:
            content = tsx_file.read_text(errors="ignore")
        except OSError:
            continue

        has_default_export = bool(
            re.search(r"export\s+default\s+(function|class|\(|const\s+\w+\s*=)", content)
        )
        has_jsx = "<" in content and "/>" in content

        if has_default_export or has_jsx:
            name = tsx_file.stem
            if not name[0].isupper():
                name = name[0].upper() + name[1:]

            named_match = re.search(r"export\s+(?:const|function|class)\s+(\w+)", content)
            if named_match and named_match.group(1)[0].isupper():
                name = named_match.group(1)

            rel = str(tsx_file.relative_to(components_dir))
            components.append({"name": name, "relative_path": rel})

    return components


def _discover_fastapi_routers(work_dir: Path) -> list[dict]:
    """
    Find Python files that define an APIRouter instance.

    Returns list of {"module": "api.listings", "name": "listings"} dicts.
    """
    routers = []
    search_dirs = [work_dir / "api", work_dir / "routers", work_dir / "app" / "api"]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for py_file in sorted(search_dir.glob("*.py")):
            if py_file.stem == "__init__":
                continue
            try:
                content = py_file.read_text(errors="ignore")
            except OSError:
                continue
            if "APIRouter" in content and "router" in content:
                rel = py_file.relative_to(work_dir)
                module = str(rel).replace("/", ".").replace(".py", "")
                routers.append({"module": module, "name": py_file.stem})

    return routers


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[bool, str, str]:
    """Run a subprocess, return (success, stdout, stderr)."""
    if not cmd:
        return True, "", ""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return False, "", f"command not found: {cmd[0]}"
