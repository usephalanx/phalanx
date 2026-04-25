"""Per-language env_detector contract tests.

Each language has a fixture repo under fixtures/<lang>/. The detector
must:
  1. Identify the stack correctly.
  2. Pick a base_image that matches the language's CI environment.
  3. Emit install_commands the engineer's sandbox can replay.
  4. Detect system_deps from `apt install` lines in workflow YAML.
  5. Surface known limitations in `notes` so the SRE agent can decide
     whether to escalate (e.g., "Phase 1: detailed env detection not
     yet implemented for stack='node'").

These assertions are the language-agnostic guard rails. When v3
gets full Node/Java/C# detection, tighten the language-specific
assertions accordingly — e.g., TS should infer pnpm vs npm from the
lockfile, Java should pick a JDK version from <maven.compiler.target>,
C# should respect global.json's SDK pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from phalanx.ci_fixer_v3.env_detector import detect_env

FIXTURES = Path(__file__).parent / "fixtures"


# ──────────────────────────────────────────────────────────────────────────
# Python — the path we have full detection for. Most assertions tighten here.
# ──────────────────────────────────────────────────────────────────────────


def test_python_detects_stack_and_base_image():
    spec = detect_env(FIXTURES / "python")
    assert spec.stack == "python", spec.stack
    # requires-python = ">=3.11" → base image must respect lower bound, not
    # forward-upgrade to 3.12. (Bug PB6 from the canary review.)
    assert spec.base_image == "python:3.11-slim", spec.base_image
    assert spec.python_version == "3.11"


def test_python_install_commands_use_extras_group():
    spec = detect_env(FIXTURES / "python")
    # pyproject's [project.optional-dependencies].tests group should be
    # picked up — engineer's coder will need pytest + pytest-cov.
    assert any('pip install -e ".[tests]"' in c for c in spec.install_commands), (
        spec.install_commands
    )
    # `pip install --upgrade pip` should always be first.
    assert spec.install_commands[0].startswith("pip install --upgrade pip")


def test_python_workflow_apt_deps_detected():
    spec = detect_env(FIXTURES / "python")
    # workflow has `sudo apt install -y gettext libxml2`
    assert "gettext" in spec.system_deps, spec.system_deps
    assert "libxml2" in spec.system_deps, spec.system_deps
    # The regex must NOT swallow shell continuations (Bug PB7).
    forbidden = {"echo", "done", "make", "&&", "&", "|", ";"}
    assert not (set(spec.system_deps) & forbidden), spec.system_deps


def test_python_ruff_modern_config_flagged():
    spec = detect_env(FIXTURES / "python")
    # pyproject uses lint.select + lint.future-annotations, which require
    # ruff >= 0.5. The detector should record this for SRE provisioning.
    assert spec.tool_versions.get("ruff") == ">=0.5"
    assert any("ruff lint.* config" in n for n in spec.notes), spec.notes


# ──────────────────────────────────────────────────────────────────────────
# TypeScript — Phase-1 returns minimal pass-through. The contract:
# - stack identified
# - base_image is a Node image (NOT defaulted to ubuntu)
# - notes flags incomplete detection so the SRE agent can be explicit
# - apt deps still picked up cross-stack from workflow YAML
# ──────────────────────────────────────────────────────────────────────────


def test_typescript_detects_stack():
    spec = detect_env(FIXTURES / "typescript")
    # Phase 1 detects 'node' for any package.json-bearing repo. When TS
    # gets its own detection, tighten this to 'typescript'.
    assert spec.stack == "node", spec.stack


def test_typescript_uses_node_base_image_not_python_or_ubuntu():
    spec = detect_env(FIXTURES / "typescript")
    # If env_detector accidentally fell through to ubuntu/python, the
    # downstream sandbox would be wrong. Lock that in.
    assert "node" in spec.base_image, (
        f"TS fixture should pick a node base image, got {spec.base_image!r}"
    )


def test_typescript_phase1_notes_flag_incomplete_detection():
    spec = detect_env(FIXTURES / "typescript")
    # Until v3 grows full Node/TS detection (pnpm vs npm vs yarn from
    # lockfile, devDependencies → install_commands, etc.), we want a
    # note so the SRE agent's logs make the gap visible.
    assert any("Phase 1" in n or "not yet implemented" in n for n in spec.notes), (
        spec.notes
    )


@pytest.mark.xfail(
    reason="TS package-manager detection lands in Phase 2 — pnpm-lock.yaml "
    "should override the npm default.",
    strict=False,
)
def test_typescript_pnpm_lockfile_picks_pnpm_install():
    """Future contract: when pnpm-lock.yaml is present, install_commands
    should use `pnpm install --frozen-lockfile`, not `npm ci`."""
    spec = detect_env(FIXTURES / "typescript")
    assert any("pnpm install" in c for c in spec.install_commands), spec.install_commands


# ──────────────────────────────────────────────────────────────────────────
# JavaScript — same Phase-1 expectations as TS but with package-lock.json
# (npm), workflow uses `npm ci` + `npm test`.
# ──────────────────────────────────────────────────────────────────────────


def test_javascript_detects_stack():
    spec = detect_env(FIXTURES / "javascript")
    assert spec.stack == "node"


def test_javascript_no_pnpm_lockfile_means_npm():
    """Sanity: this fixture has package-lock.json (npm), not pnpm-lock.yaml.
    The detector shouldn't flag pnpm in any future Phase-2 work for it."""
    spec = detect_env(FIXTURES / "javascript")
    # No phantom pnpm reference once Phase 2 lands.
    for n in spec.notes:
        assert "pnpm" not in n.lower(), f"unexpected pnpm reference in notes: {n!r}"


# ──────────────────────────────────────────────────────────────────────────
# Java — Phase-1 returns minimal. The contract:
# - stack identified as 'java'
# - base_image is a JDK-bearing image, not python
# - notes flag the incomplete detection
# Future tighten: parse <maven.compiler.target> and use matching JDK image.
# ──────────────────────────────────────────────────────────────────────────


def test_java_detects_stack():
    spec = detect_env(FIXTURES / "java")
    assert spec.stack == "java"


def test_java_uses_jdk_base_image():
    spec = detect_env(FIXTURES / "java")
    # Has to be a JDK-bearing image. Maven/Gradle will fail without javac.
    img = spec.base_image.lower()
    assert any(k in img for k in ("jdk", "temurin", "openjdk", "maven")), spec.base_image


@pytest.mark.xfail(
    reason="JDK version pinning from <maven.compiler.target> lands in Phase 2.",
    strict=False,
)
def test_java_jdk_version_from_pom():
    """Future contract: pom.xml's <maven.compiler.target>17</> should pin
    the base image to a JDK17 variant, not the latest LTS."""
    spec = detect_env(FIXTURES / "java")
    assert "17" in spec.base_image, spec.base_image


# ──────────────────────────────────────────────────────────────────────────
# C# — Phase-1 returns minimal. The contract:
# - stack identified as 'csharp'
# - base_image is a .NET SDK image (large; NOT slim)
# Future tighten: read global.json sdk.version and pick matching SDK tag.
# ──────────────────────────────────────────────────────────────────────────


def test_csharp_detects_stack():
    spec = detect_env(FIXTURES / "csharp")
    assert spec.stack == "csharp"


def test_csharp_uses_dotnet_sdk_image():
    spec = detect_env(FIXTURES / "csharp")
    img = spec.base_image.lower()
    assert "dotnet" in img or "mcr.microsoft.com" in img, spec.base_image


@pytest.mark.xfail(
    reason="global.json SDK version pinning lands in Phase 2.",
    strict=False,
)
def test_csharp_sdk_version_from_global_json():
    """Future contract: global.json sdk.version=8.0.100 should pin the
    image to dotnet/sdk:8.0, not the latest."""
    spec = detect_env(FIXTURES / "csharp")
    assert "8.0" in spec.base_image, spec.base_image


# ──────────────────────────────────────────────────────────────────────────
# Cross-language: tools the detector must NOT mistake for system_deps.
# Workflow YAMLs frequently chain `apt install X && do_something_else`,
# `apt install Y; cleanup`, etc. The regex must stop at shell terminators.
# (Regression for review PB7.)
# ──────────────────────────────────────────────────────────────────────────


_SHELL_NOISE = {"echo", "done", "&&", "&", "|", ";", "\\"}


@pytest.mark.parametrize("lang", ["python", "typescript", "javascript", "java", "csharp"])
def test_apt_regex_does_not_swallow_shell_noise(lang: str):
    spec = detect_env(FIXTURES / lang)
    leaked = set(spec.system_deps) & _SHELL_NOISE
    assert not leaked, f"{lang}: apt regex leaked shell noise: {leaked}"
