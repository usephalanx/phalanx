"""CI Fixer v2 — single-agent + tools + loop.

Replaces the legacy deterministic pipeline at `phalanx/ci_fixer/` (spec §0).
Do NOT import from this package in production paths until the feature flag
`settings.phalanx_ci_fixer_v2_enabled` is True.

Spec: docs/ci-fixer-v2-spec.md
"""

__version__ = "0.1.0-alpha"
