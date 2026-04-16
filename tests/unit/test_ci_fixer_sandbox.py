"""
Tests for phalanx.ci_fixer.sandbox — SandboxProvisioner + SandboxResult.

Coverage targets:
  - detect_stack: all 5 stacks (python/node/go/rust/unknown) + priority order
  - provision: happy path, disabled, unique IDs, stack_hint bypass
  - SandboxResult: field defaults
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from phalanx.ci_fixer.sandbox import SandboxProvisioner, SandboxResult

if TYPE_CHECKING:
    from pathlib import Path

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_workspace(tmp_path: Path, *filenames: str) -> Path:
    """Create a temp directory with the given marker files."""
    for name in filenames:
        (tmp_path / name).touch()
    return tmp_path


# ── detect_stack ──────────────────────────────────────────────────────────────


class TestDetectStack:
    def test_detect_stack_python_pyproject(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "pyproject.toml")
        assert SandboxProvisioner().detect_stack(ws) == "python"

    def test_detect_stack_python_requirements(self, tmp_path: Path):
        """requirements.txt alone should also detect python."""
        ws = _make_workspace(tmp_path, "requirements.txt")
        assert SandboxProvisioner().detect_stack(ws) == "python"

    def test_detect_stack_python_setup_py(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "setup.py")
        assert SandboxProvisioner().detect_stack(ws) == "python"

    def test_detect_stack_node(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "package.json")
        assert SandboxProvisioner().detect_stack(ws) == "node"

    def test_detect_stack_go(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "go.mod")
        assert SandboxProvisioner().detect_stack(ws) == "go"

    def test_detect_stack_rust(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "Cargo.toml")
        assert SandboxProvisioner().detect_stack(ws) == "rust"

    def test_detect_stack_unknown(self, tmp_path: Path):
        """Empty workspace has no markers → unknown."""
        assert SandboxProvisioner().detect_stack(tmp_path) == "unknown"

    def test_detect_stack_python_wins_over_node(self, tmp_path: Path):
        """Python is checked first — monorepo with both pyproject + package.json resolves to python."""
        ws = _make_workspace(tmp_path, "pyproject.toml", "package.json")
        assert SandboxProvisioner().detect_stack(ws) == "python"

    def test_detect_stack_nonexistent_path(self, tmp_path: Path):
        """Path that doesn't exist returns unknown without raising."""
        missing = tmp_path / "nonexistent"
        result = SandboxProvisioner().detect_stack(missing)
        assert result == "unknown"


# ── SandboxProvisioner.provision ──────────────────────────────────────────────


class TestSandboxProvision:
    @pytest.mark.asyncio
    async def test_provision_returns_sandbox_result(self, tmp_path: Path):
        """Happy path: returns a SandboxResult with correct fields."""
        ws = _make_workspace(tmp_path, "pyproject.toml")
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(ws)

        assert result is not None
        assert result.stack == "python"
        assert result.image == "python:3.12-slim"
        assert result.workspace_path == str(ws)
        assert result.sandbox_id.startswith("phalanx-sandbox-")
        assert len(result.sandbox_id) == len("phalanx-sandbox-") + 8

    @pytest.mark.asyncio
    async def test_provision_disabled_returns_none(self, tmp_path: Path):
        """sandbox_enabled=False → provision returns None immediately."""
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = False

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(tmp_path)

        assert result is None

    @pytest.mark.asyncio
    async def test_provision_generates_unique_ids(self, tmp_path: Path):
        """Each provision call generates a different sandbox_id."""
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            p = SandboxProvisioner()
            r1 = await p.provision(tmp_path)
            r2 = await p.provision(tmp_path)

        assert r1 is not None
        assert r2 is not None
        assert r1.sandbox_id != r2.sandbox_id

    @pytest.mark.asyncio
    async def test_sandbox_result_available_true_by_default(self, tmp_path: Path):
        """SandboxResult.available defaults to True."""
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(tmp_path)

        assert result is not None
        assert result.available is True

    @pytest.mark.asyncio
    async def test_provision_stack_hint_overrides_detection(self, tmp_path: Path):
        """stack_hint bypasses file-existence detection."""
        # No marker files — would be "unknown" without the hint
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(tmp_path, stack_hint="node")

        assert result is not None
        assert result.stack == "node"
        assert result.image == "node:20-slim"

    @pytest.mark.asyncio
    async def test_provision_unknown_stack_uses_ubuntu(self, tmp_path: Path):
        """Empty workspace → unknown stack → ubuntu:22.04 image."""
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(tmp_path)

        assert result is not None
        assert result.stack == "unknown"
        assert result.image == "ubuntu:22.04"

    @pytest.mark.asyncio
    async def test_provision_go_workspace(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "go.mod")
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(ws)

        assert result is not None
        assert result.stack == "go"
        assert result.image == "golang:1.22-alpine"

    @pytest.mark.asyncio
    async def test_provision_rust_workspace(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "Cargo.toml")
        mock_settings = MagicMock()
        mock_settings.sandbox_enabled = True

        with patch("phalanx.ci_fixer.sandbox.settings", mock_settings):
            result = await SandboxProvisioner().provision(ws)

        assert result is not None
        assert result.stack == "rust"
        assert result.image == "rust:1.77-slim"


# ── SandboxResult dataclass ───────────────────────────────────────────────────


class TestSandboxResult:
    def test_sandbox_result_extra_defaults_empty(self):
        r = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path="/tmp/ws",
        )
        assert r.extra == {}

    def test_sandbox_result_available_default(self):
        r = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path="/tmp/ws",
        )
        assert r.available is True

    def test_sandbox_result_available_can_be_false(self):
        r = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path="/tmp/ws",
            available=False,
        )
        assert r.available is False
