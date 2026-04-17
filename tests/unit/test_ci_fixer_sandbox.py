"""
Tests for phalanx.ci_fixer.sandbox — SandboxProvisioner + SandboxResult.

Coverage targets:
  - detect_stack: all 5 stacks (python/node/go/rust/unknown) + priority order
  - provision: happy path with pool checkout, disabled, unique IDs, stack_hint
  - provision: pool checkout timeout → available=False fallback
  - provision: Docker error → available=False fallback
  - release: container_id empty (no-op), container_id set → pool.checkin
  - SandboxResult: field defaults including new container_id + mount_path
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer.sandbox import SandboxProvisioner, SandboxResult
from phalanx.ci_fixer.sandbox_pool import SandboxUnavailableError

if TYPE_CHECKING:
    from pathlib import Path


def _mock_pool(container_id: str = "ctr-abc123") -> MagicMock:
    """Return a mock SandboxPool that returns a container on checkout."""
    from phalanx.ci_fixer.sandbox_pool import PooledContainer

    pool = MagicMock()
    container = PooledContainer(
        container_id=container_id,
        stack="python",
        image="phalanx-sandbox-python:latest",
    )
    pool.checkout = AsyncMock(return_value=container)
    pool.checkin = AsyncMock()
    return pool

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
    def _mock_settings(self, enabled: bool = True) -> MagicMock:
        s = MagicMock()
        s.sandbox_enabled = enabled
        s.sandbox_checkout_timeout_seconds = 30
        return s

    @pytest.mark.asyncio
    async def test_provision_returns_sandbox_result_with_container_id(self, tmp_path: Path):
        """Happy path: pool checkout succeeds → SandboxResult has container_id set."""
        ws = _make_workspace(tmp_path, "pyproject.toml")
        pool = _mock_pool(container_id="ctr-abc123")

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                provisioner = SandboxProvisioner()
                with patch.object(provisioner, "_bind_workspace", new_callable=AsyncMock):
                    result = await provisioner.provision(ws)

        assert result is not None
        assert result.stack == "python"
        assert result.image == "python:3.12-slim"
        assert result.workspace_path == str(ws)
        assert result.sandbox_id.startswith("phalanx-sandbox-")
        assert result.container_id == "ctr-abc123"
        assert result.available is True

    @pytest.mark.asyncio
    async def test_provision_disabled_returns_none(self, tmp_path: Path):
        """sandbox_enabled=False → provision returns None immediately."""
        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings(enabled=False)):
            result = await SandboxProvisioner().provision(tmp_path)

        assert result is None

    @pytest.mark.asyncio
    async def test_provision_generates_unique_ids(self, tmp_path: Path):
        """Each provision call generates a different sandbox_id."""
        pool = _mock_pool()

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                p = SandboxProvisioner()
                with patch.object(p, "_bind_workspace", new_callable=AsyncMock):
                    r1 = await p.provision(tmp_path)
                    r2 = await p.provision(tmp_path)

        assert r1 is not None and r2 is not None
        assert r1.sandbox_id != r2.sandbox_id

    @pytest.mark.asyncio
    async def test_provision_pool_timeout_returns_available_false(self, tmp_path: Path):
        """Pool checkout times out → SandboxResult with available=False, no exception."""
        pool = MagicMock()
        pool.checkout = AsyncMock(side_effect=SandboxUnavailableError("timeout"))

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                result = await SandboxProvisioner().provision(tmp_path)

        assert result is not None
        assert result.available is False
        assert result.container_id == ""

    @pytest.mark.asyncio
    async def test_provision_docker_error_returns_available_false(self, tmp_path: Path):
        """Any unexpected exception → SandboxResult with available=False."""
        pool = MagicMock()
        pool.checkout = AsyncMock(side_effect=RuntimeError("docker daemon not found"))

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                result = await SandboxProvisioner().provision(tmp_path)

        assert result is not None
        assert result.available is False

    @pytest.mark.asyncio
    async def test_provision_stack_hint_overrides_detection(self, tmp_path: Path):
        """stack_hint bypasses file-existence detection."""
        pool = _mock_pool()

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                provisioner = SandboxProvisioner()
                with patch.object(provisioner, "_bind_workspace", new_callable=AsyncMock):
                    result = await provisioner.provision(tmp_path, stack_hint="node")

        assert result is not None
        assert result.stack == "node"
        assert result.image == "node:20-slim"

    @pytest.mark.asyncio
    async def test_provision_unknown_stack_uses_ubuntu(self, tmp_path: Path):
        """Empty workspace → unknown stack → ubuntu:22.04 image."""
        pool = _mock_pool()

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                provisioner = SandboxProvisioner()
                with patch.object(provisioner, "_bind_workspace", new_callable=AsyncMock):
                    result = await provisioner.provision(tmp_path)

        assert result is not None
        assert result.stack == "unknown"
        assert result.image == "ubuntu:22.04"

    @pytest.mark.asyncio
    async def test_provision_go_workspace(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "go.mod")
        pool = _mock_pool()

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                provisioner = SandboxProvisioner()
                with patch.object(provisioner, "_bind_workspace", new_callable=AsyncMock):
                    result = await provisioner.provision(ws)

        assert result is not None
        assert result.stack == "go"
        assert result.image == "golang:1.22-alpine"

    @pytest.mark.asyncio
    async def test_provision_rust_workspace(self, tmp_path: Path):
        ws = _make_workspace(tmp_path, "Cargo.toml")
        pool = _mock_pool()

        with patch("phalanx.ci_fixer.sandbox.settings", self._mock_settings()):
            with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
                provisioner = SandboxProvisioner()
                with patch.object(provisioner, "_bind_workspace", new_callable=AsyncMock):
                    result = await provisioner.provision(ws)

        assert result is not None
        assert result.stack == "rust"
        assert result.image == "rust:1.77-slim"


class TestSandboxProvisionerRelease:
    @pytest.mark.asyncio
    async def test_release_no_op_when_no_container_id(self, tmp_path: Path):
        """release() with empty container_id is a no-op — no pool call."""
        result = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path=str(tmp_path),
            container_id="",
        )
        pool = MagicMock()
        pool.checkin = AsyncMock()

        with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
            await SandboxProvisioner().release(result)

        pool.checkin.assert_not_called()

    @pytest.mark.asyncio
    async def test_release_calls_pool_checkin(self, tmp_path: Path):
        """release() with container_id → pool.checkin called."""
        result = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="phalanx-sandbox-python:latest",
            workspace_path=str(tmp_path),
            container_id="ctr-abc123",
        )
        pool = MagicMock()
        pool.checkin = AsyncMock()

        with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
            await SandboxProvisioner().release(result)

        pool.checkin.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_swallows_pool_error(self, tmp_path: Path):
        """pool.checkin raises → release() swallows the error."""
        result = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="img",
            workspace_path=str(tmp_path),
            container_id="ctr-abc123",
        )
        pool = MagicMock()
        pool.checkin = AsyncMock(side_effect=RuntimeError("pool gone"))

        with patch("phalanx.ci_fixer.sandbox.get_sandbox_pool", AsyncMock(return_value=pool)):
            await SandboxProvisioner().release(result)  # must not raise


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

    def test_sandbox_result_container_id_default_empty(self):
        r = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path="/tmp/ws",
        )
        assert r.container_id == ""

    def test_sandbox_result_mount_path_default(self):
        r = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path="/tmp/ws",
        )
        assert r.mount_path == "/workspace"

    def test_sandbox_result_container_id_set(self):
        r = SandboxResult(
            sandbox_id="phalanx-sandbox-abc12345",
            stack="python",
            image="python:3.12-slim",
            workspace_path="/tmp/ws",
            container_id="abc123def456",
        )
        assert r.container_id == "abc123def456"
