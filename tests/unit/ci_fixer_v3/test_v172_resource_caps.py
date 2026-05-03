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

    def test_cap_drop_NOT_applied_due_to_apt_incompatibility(self, captured_argv):
        """v1.7.2.1 rollback: --cap-drop=ALL broke apt-get in the testbed
        (Operation not permitted on every URI fetch). Re-applying caps
        deferred to v1.7.3 where we pre-bake apt deps into the base image.
        Test pins the rollback so we don't re-introduce the breaking flags
        without the v1.7.3 prerequisite work."""
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--cap-drop" not in args, (
            "v1.7.2.1 rolled back cap-drop; do not re-add without v1.7.3 prereq"
        )
        assert "no-new-privileges:true" not in args, (
            "v1.7.2.1 rolled back no-new-privileges; do not re-add without v1.7.3 prereq"
        )

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

    def test_cap_add_NOT_applied_due_to_rollback(self, captured_argv):
        """Same v1.7.2.1 rollback rationale — cap-add lines were tied to
        cap-drop=ALL. Without the drop, no need to add. Test pins the
        rolled-back state."""
        asyncio.run(_docker_run_detached("python:3.12-slim"))
        args = captured_argv["args"]
        assert "--cap-add" not in args


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
