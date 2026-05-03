"""Tier-1 tests for v1.7.2 resource cap hardening.

Verifies the docker-run argv contains the v1.7.2 hardening flags. Doesn't
spin a real container — just intercepts subprocess invocation.

Tier-2 integration test (separate file, gated by env) will spin a real
container and confirm fork-bomb / memory-hog actually fail cleanly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer_v3.provisioner import _docker_run_detached


class TestDockerRunHardeningArgs:
    """Inspect the argv passed to subprocess; assert each hardening flag
    is present."""

    @pytest.fixture
    def captured_argv(self):
        """Patch asyncio.create_subprocess_exec, capture argv, return mock proc."""
        captured = {}

        async def _fake_create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(
                return_value=(b"abc123def456containerid\n", b"")
            )
            return mock_proc

        with patch(
            "phalanx.ci_fixer_v3.provisioner.asyncio.create_subprocess_exec",
            side_effect=_fake_create,
        ):
            yield captured

    def test_pids_limit_set(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--pids-limit" in args
        idx = args.index("--pids-limit")
        assert args[idx + 1] == "256", f"pids-limit should be 256; got {args[idx+1]}"

    def test_swap_disabled(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--memory-swap" in args
        idx = args.index("--memory-swap")
        # When --memory-swap == --memory, swap is disabled
        assert args[idx + 1] == "2g"

    def test_cap_drop_all(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--cap-drop" in args
        # Multiple --cap-drop / --cap-add can appear; at least one ALL
        assert "ALL" in args

    def test_no_new_privileges(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--security-opt" in args
        assert "no-new-privileges:true" in args

    def test_ulimit_nofile_set(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        # --ulimit nofile=4096:4096 — we pass it as flag + value pair
        # so look for the value string somewhere
        assert any(
            isinstance(a, str) and "nofile" in a for a in args
        )

    def test_ulimit_nproc_set(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert any(
            isinstance(a, str) and "nproc" in a for a in args
        )

    def test_cap_add_minimal_for_apt(self, captured_argv):
        """We drop ALL caps then add back only the minimal set apt needs.
        Verify we ADD back DAC_OVERRIDE (write to system paths) and SETUID
        (apt drops privs) but NOT broader caps like SYS_ADMIN."""
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        # Filter to just --cap-add value pairs
        cap_adds = [args[i+1] for i, a in enumerate(args) if a == "--cap-add"]
        assert "DAC_OVERRIDE" in cap_adds
        assert "SETUID" in cap_adds
        assert "SYS_ADMIN" not in cap_adds
        assert "NET_ADMIN" not in cap_adds


class TestBackwardsCompatibility:
    """Ensure the new flags didn't break the existing baseline behavior."""

    @pytest.fixture
    def captured_argv(self):
        captured = {}

        async def _fake_create(*args, **kwargs):
            captured["args"] = args
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(
                return_value=(b"deadbeef" * 4 + b"\n", b"")
            )
            return mock_proc

        with patch(
            "phalanx.ci_fixer_v3.provisioner.asyncio.create_subprocess_exec",
            side_effect=_fake_create,
        ):
            yield captured

    def test_existing_memory_cap_preserved(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--memory" in args
        idx = args.index("--memory")
        assert args[idx + 1] == "2g"

    def test_existing_cpu_cap_preserved(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--cpus" in args
        idx = args.index("--cpus")
        assert args[idx + 1] == "2"

    def test_network_bridge_preserved(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--network" in args
        idx = args.index("--network")
        assert args[idx + 1] == "bridge"

    def test_rm_flag_preserved(self, captured_argv):
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--rm" in args

    def test_returns_container_id_on_success(self, captured_argv):
        container_id, err = asyncio.run(_docker_run_detached("python:3.12-slim"))
        assert err is None
        assert container_id is not None
        # Provisioner truncates to 12 chars
        assert len(container_id) == 12
