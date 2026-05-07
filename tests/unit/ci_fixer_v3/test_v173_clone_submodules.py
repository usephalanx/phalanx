"""v1.7.3 post-Phase-2b — submodule init in _clone_workspace.

Phase 2b F2 attempt #2 (aio-libs/aiohttp) failed at `pip install .`
with the error:

    error: subprocess-exited-with-error
    × Getting requirements to build wheel did not run successfully.
      [3 lines of output]
      Install submodules when building from git clone
      Hint:
        git submodule update --init

aiohttp uses git submodules; the existing _clone_workspace did a
shallow clone WITHOUT initializing submodules, so subsequent
`pip install .` couldn't build the wheel.

Fix: best-effort `git submodule update --init --recursive --depth=1`
on the workspace post-clone, no-op when .gitmodules is absent.

Tests:
  - workspace WITHOUT .gitmodules → init helper is a no-op
  - workspace WITH .gitmodules → submodule update ran (mock)
  - workspace where init fails → no exception raised (best-effort)
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from phalanx.agents.cifix_sre import _init_submodules_if_present


# ── No .gitmodules → no-op ────────────────────────────────────────────


class TestNoGitmodules:
    def test_helper_is_noop_when_gitmodules_absent(self):
        """Most repos don't have submodules. The helper should
        NOT call into git at all in that case."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            # No .gitmodules file
            with patch("git.Repo") as mock_repo:
                _init_submodules_if_present(workspace)
                mock_repo.assert_not_called()


# ── .gitmodules present → submodule update fires ─────────────────────


class TestWithGitmodules:
    def test_submodule_update_called_when_gitmodules_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".gitmodules").write_text("[submodule \"x\"]\n")

            mock_repo = MagicMock()
            with patch("git.Repo", return_value=mock_repo):
                _init_submodules_if_present(workspace)

            mock_repo.git.submodule.assert_called_once_with(
                "update", "--init", "--recursive", "--depth=1"
            )

    def test_submodule_init_failure_does_not_raise(self):
        """Best-effort: a submodule fetch failure is logged but the
        helper still returns cleanly. The downstream pip install will
        surface the same error if submodules were truly required."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".gitmodules").write_text("[submodule \"x\"]\n")

            mock_repo = MagicMock()
            mock_repo.git.submodule.side_effect = RuntimeError("network err")
            with patch("git.Repo", return_value=mock_repo):
                # Must not raise
                _init_submodules_if_present(workspace)


# ── End-to-end: real git submodule init on a synthetic repo ───────────


class TestEndToEndOnRealGit:
    """Verifies the helper actually invokes git correctly against a
    real (synthetic) repo. No network needed — we just check the helper
    completes cleanly when .gitmodules is present but its submodules
    aren't fetchable (no remote)."""

    def test_real_git_repo_with_unreachable_submodule_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            # Initialize a real git repo
            subprocess.run(
                ["git", "init", "-q"], cwd=workspace, check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@phalanx.local"],
                cwd=workspace, check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test-bot"],
                cwd=workspace, check=True,
            )
            # Add a fake .gitmodules pointing at an unreachable repo
            (workspace / ".gitmodules").write_text(
                '[submodule "vendor/x"]\n'
                "    path = vendor/x\n"
                "    url = https://example.invalid/notarepo.git\n"
            )
            # Actually attempt the helper — the submodule fetch WILL
            # fail (network unreachable), but the helper must not raise.
            _init_submodules_if_present(workspace)
            # No exception → test passes.
