"""v1.7.3 post-Phase-2b — provisioner baseline apt deps.

Phase 2b F2 attempt #3 (aio-libs/aiohttp) failed at wheel-build time
with `gcc: No such file or directory`. The python:3.12-slim base
image has Python headers but no gcc. Repos with C extensions
(aiohttp _websocket/mask.c, _http_parser.pyx, etc.) can't compile.

Fix: add `build-essential` to _BASELINE_APT_DEPS so every fresh
sandbox has gcc + g++ + make + libc6-dev available. ~250 MB image
bloat for universal C-extension support.

Tests:
  - build-essential in the baseline tuple
  - other essentials still present (git, ca-certificates, curl)
  - tuple shape stable (no duplicates, all strings)
"""

from __future__ import annotations

from phalanx.ci_fixer_v3.provisioner import _BASELINE_APT_DEPS


class TestBaselineAptDeps:
    def test_build_essential_is_baseline(self):
        """v1.7.3 NM4 — gcc must be available for C-extension wheel
        builds. build-essential pulls in gcc + g++ + make + libc6-dev."""
        assert "build-essential" in _BASELINE_APT_DEPS

    def test_existing_baseline_deps_preserved(self):
        """Don't accidentally drop any of the original deps when
        adding new ones."""
        for required in ("git", "ca-certificates", "curl"):
            assert required in _BASELINE_APT_DEPS, (
                f"baseline missing {required!r} — agents / installers depend on it"
            )

    def test_baseline_is_tuple_of_strings(self):
        """Stability check: tuple, not list (immutable); strings only."""
        assert isinstance(_BASELINE_APT_DEPS, tuple)
        assert all(isinstance(p, str) and p for p in _BASELINE_APT_DEPS)

    def test_baseline_has_no_duplicates(self):
        assert len(_BASELINE_APT_DEPS) == len(set(_BASELINE_APT_DEPS))
