"""v1.7.3 runtime hardening — infra-vs-architecture failure classification.

These VERDICTS are written to runs.failure_class when the orchestration
layer (commander watchdog, stuck-task detector) terminates a run for
infrastructure reasons rather than for any reasoning-quality property.

The shadow runner reads these into the ledger. Aggregate metrics treat
infra failures separately from architecture refusals (SAFE_ESCALATE)
and engineer failures (FAILED) so a noisy Celery/Docker hour doesn't
poison the hit-rate signal.
"""

from __future__ import annotations

# ── Infra failure classes — set by commander/detector, never by an agent ──

FAILED_INFRA_TIMEOUT = "FAILED_INFRA_TIMEOUT"
"""Run wall-clock exceeded the commander's hard cap. Different from a
single-task timeout — this is the run-level guardrail."""

FAILED_INFRA_WORKER_HANG = "FAILED_INFRA_WORKER_HANG"
"""A specific task's heartbeat went stale beyond its TTL. The
stuck-task detector marked the task TIMED_OUT and propagated the run
to FAILED_INFRA_WORKER_HANG."""

FAILED_SANDBOX_SETUP = "FAILED_SANDBOX_SETUP"
"""SRE setup couldn't provision a sandbox (Docker pull failure, image
build crash, exec timeout). Distinct from `FAILED_TL` — the LLM agents
never got a chance to run.

P1-6: subclassed when the failing step's phase can be identified.
The bare class is retained only for pre-P1-6 ledger rows; new rows
emit one of the FAILED_SANDBOX_SETUP_* subclasses below."""

# ── P1-6 sub-classes by failing-phase ──
FAILED_SANDBOX_SETUP_APT = "FAILED_SANDBOX_SETUP_APT"
"""apt-get install failed during sandbox provisioning."""

FAILED_SANDBOX_SETUP_PIP = "FAILED_SANDBOX_SETUP_PIP"
"""pip install failed during sandbox provisioning."""

FAILED_SANDBOX_SETUP_UV = "FAILED_SANDBOX_SETUP_UV"
"""uv sync / uv pip install failed during sandbox provisioning."""

FAILED_SANDBOX_SETUP_GIT = "FAILED_SANDBOX_SETUP_GIT"
"""git clone / git submodule failed during sandbox provisioning."""

FAILED_SANDBOX_SETUP_UNKNOWN = "FAILED_SANDBOX_SETUP_UNKNOWN"
"""Sandbox setup failed but the phase couldn't be identified (e.g.
docker_run_failed, mkdir_workspace, or any other pre-install step)."""

FAILED_SANDBOX_CLEANUP = "FAILED_SANDBOX_CLEANUP"
"""Cleanup itself failed (rare — Docker daemon dead). Logged but does
NOT change the verdict if the run had already terminated cleanly; this
is bookkeeping, not a verdict override."""

# ── Architecture failure classes — set by agents themselves ──

FAILED_TL = "FAILED_TL"
"""TL produced no fix_spec OR plan validator rejected (non-calibration
sub-cases). Distinct from SAFE_ESCALATE which is a confidence/calibration
refusal."""

FAILED_ENGINEER = "FAILED_ENGINEER"
"""Engineer ran but couldn't verify a green sandbox. Architecture
worked correctly; the proposed change just didn't pass tests."""

FAILED_SRE_VERIFY = "FAILED_SRE_VERIFY"
"""SRE verify reported new_failures even after engineer claimed verified.
Different from FAILED_SANDBOX_SETUP."""

# ── Helpers ──

INFRA_FAILURE_CLASSES = frozenset(
    {
        FAILED_INFRA_TIMEOUT,
        FAILED_INFRA_WORKER_HANG,
        FAILED_SANDBOX_SETUP,
        FAILED_SANDBOX_SETUP_APT,
        FAILED_SANDBOX_SETUP_PIP,
        FAILED_SANDBOX_SETUP_UV,
        FAILED_SANDBOX_SETUP_GIT,
        FAILED_SANDBOX_SETUP_UNKNOWN,
        FAILED_SANDBOX_CLEANUP,
    }
)

# Map phase string → subclass constant. Used by runner._resolve_failure_class.
SANDBOX_PHASE_TO_FAILURE_CLASS: dict[str, str] = {
    "apt": FAILED_SANDBOX_SETUP_APT,
    "pip": FAILED_SANDBOX_SETUP_PIP,
    "uv": FAILED_SANDBOX_SETUP_UV,
    "git": FAILED_SANDBOX_SETUP_GIT,
    "infra": FAILED_SANDBOX_SETUP_UNKNOWN,
    "unknown": FAILED_SANDBOX_SETUP_UNKNOWN,
}

ARCHITECTURE_FAILURE_CLASSES = frozenset(
    {
        FAILED_TL,
        FAILED_ENGINEER,
        FAILED_SRE_VERIFY,
    }
)


def is_infra_failure(failure_class: str | None) -> bool:
    """True iff the run died for infrastructure reasons. Aggregate
    metrics use this to separate noise from signal."""
    return failure_class in INFRA_FAILURE_CLASSES


def is_architecture_failure(failure_class: str | None) -> bool:
    """True iff the run died for reasoning/architecture reasons."""
    return failure_class in ARCHITECTURE_FAILURE_CLASSES
