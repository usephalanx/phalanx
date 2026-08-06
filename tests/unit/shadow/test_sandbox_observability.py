"""P1-6 — sandbox observability: every FAILED_SANDBOX_SETUP ledger row
must be actionable from the ledger alone.

Tests:
  - detector returns structured diagnostic on the canonical
    sandbox_provisioning_failed shape
  - detector returns None on healthy / non-SRE failures / unrelated SRE failures
  - synthesizer produces an operator-actionable one-liner
  - build_provenance includes sre_setup_diagnostic
  - schema version is bumped to 2
  - reconciler preserves the diagnostic path
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from phalanx.shadow import provenance as prov_mod
from phalanx.shadow.provenance import (
    PROVENANCE_SCHEMA_VERSION,
    build_provenance,
    synthesize_root_cause_for_sandbox_setup,
)
from phalanx.shadow.runner import _detect_sre_setup_failure


def _sre_failed_task(*, error_message: str, base_image: str = "python:3.12-slim",
                    install_commands: list[str] | None = None,
                    setup_log: list | None = None,
                    task_id: str = "sre-task-1") -> SimpleNamespace:
    """Build a stand-in for a FAILED cifix_sre_setup Task row that mirrors
    what cifix_sre.py:152-162 writes when provision_on_the_fly fails."""
    return SimpleNamespace(
        id=task_id,
        agent_role="cifix_sre_setup",
        status="FAILED",
        error=f"sandbox_provisioning_failed: {error_message}",
        output={
            "mode": "setup",
            "error": error_message,
            "env_spec": {
                "base_image": base_image,
                "stack": "python",
                "install_commands": install_commands or [],
            },
            "setup_log": setup_log or [],
        },
        sequence_num=1,
        created_at=None,
    )


def _setup_step(*, step: str, cmd: str, ok: bool, exit_code: int = 0,
                stderr: str = "", stdout: str = "") -> dict:
    """A single provisioner setup_log entry, post-P1-6-v2 shape."""
    return {
        "step": step,
        "cmd": cmd,
        "ok": ok,
        "exit_code": exit_code,
        "error": stderr,
        "stdout_tail": stdout,
    }


# ── Detector returns structured diagnostic ───────────────────────────────────


class TestDetectorReturnsDiagnostic:
    def test_canonical_docker_socket_failure(self):
        """The exact shape from W2 verification: docker socket permission.
        setup_log is empty because docker_run failed BEFORE any step ran."""
        task = _sre_failed_task(
            error_message=(
                "docker_run_failed: permission denied while trying to "
                "connect to the docker API at unix:///var/run/docker.sock"
            ),
            base_image="python:3.12-slim",
            install_commands=["./misc/trigger_wheel_build.sh"],
            setup_log=[],
        )
        diag = _detect_sre_setup_failure([task])
        assert diag is not None
        # P1-6 v2: failed_step derived from error prefix when setup_log empty.
        assert diag["failed_step"] == "docker_run_failed"
        assert diag["failed_command"] == "docker run"
        assert diag["phase"] == "infra"
        assert diag["failure_subclass"] == "FAILED_SANDBOX_SETUP_UNKNOWN"
        # Pre-install failure → no exit_code available at this layer.
        assert diag["exit_code"] is None
        assert "permission denied" in diag["stderr_tail"]
        assert "permission denied" in diag["error_message"]
        assert diag["base_image"] == "python:3.12-slim"
        assert diag["stack"] == "python"
        assert diag["install_commands"] == ["./misc/trigger_wheel_build.sh"]
        assert diag["sre_task_id"] == "sre-task-1"

    def test_returns_none_when_no_sre_task(self):
        # tl-only task chain, no SRE failure
        tasks = [SimpleNamespace(
            agent_role="cifix_techlead", status="COMPLETED",
            error=None, output={}, id="t1", sequence_num=1, created_at=None,
        )]
        assert _detect_sre_setup_failure(tasks) is None

    def test_returns_none_on_empty_task_list(self):
        assert _detect_sre_setup_failure([]) is None
        assert _detect_sre_setup_failure(None) is None  # type: ignore[arg-type]

    def test_returns_none_on_sre_failure_without_provisioning_marker(self):
        """SRE can fail for other reasons (sre_blocked, etc.). Those are
        NOT FAILED_SANDBOX_SETUP — they're a separate failure class.
        Detector must NOT match them."""
        task = SimpleNamespace(
            id="t1", agent_role="cifix_sre_setup", status="FAILED",
            error="sre_blocked: agentic gap-fill could not resolve",
            output={"final_status": "BLOCKED"},
            sequence_num=1, created_at=None,
        )
        assert _detect_sre_setup_failure([task]) is None

    def test_returns_none_on_completed_sre_task(self):
        task = SimpleNamespace(
            id="t1", agent_role="cifix_sre_setup", status="COMPLETED",
            error=None, output={"mode": "setup"},
            sequence_num=1, created_at=None,
        )
        assert _detect_sre_setup_failure([task]) is None

    def test_error_message_truncated_to_500(self):
        long = "x" * 1000
        task = _sre_failed_task(error_message=long)
        diag = _detect_sre_setup_failure([task])
        assert diag is not None
        assert len(diag["error_message"]) <= 500


# ── Synthesizer produces operator-actionable text ────────────────────────────


class TestSynthesizer:
    def test_docker_socket_synthesis(self):
        diag = {
            "phase": "infra",
            "failed_step": "docker_run_failed",
            "failed_command": "docker run",
            "exit_code": None,
            "stderr_tail": (
                "permission denied while trying to connect to the "
                "docker API at unix:///var/run/docker.sock"
            ),
            "error_message": "docker_run_failed: permission denied",
            "base_image": "python:3.12-slim",
        }
        txt = synthesize_root_cause_for_sandbox_setup(diag)
        assert "infra" in txt
        assert "docker run" in txt
        assert "permission denied" in txt
        assert "python:3.12-slim" in txt

    def test_synthesis_includes_exit_code_when_known(self):
        diag = {
            "phase": "apt",
            "failed_step": "apt_install_baseline",
            "failed_command": "apt-get install -y build-essential",
            "exit_code": 100,
            "stderr_tail": "E: Unable to locate package build-essential",
            "base_image": "python:3.12-slim",
        }
        txt = synthesize_root_cause_for_sandbox_setup(diag)
        assert "exit_code=100" in txt
        assert "apt" in txt
        assert "Unable to locate" in txt

    def test_synthesis_truncated_to_600(self):
        diag = {
            "phase": "pip",
            "failed_step": "install_command",
            "failed_command": "pip install " + "x" * 5000,
            "exit_code": 1,
            "stderr_tail": "y" * 5000,
            "base_image": "python:3.12-slim",
        }
        txt = synthesize_root_cause_for_sandbox_setup(diag)
        assert len(txt) <= 600

    def test_synthesis_with_minimal_fields(self):
        diag = {"phase": "unknown", "failed_step": None, "error_message": "boom",
                "base_image": None, "stderr_tail": "boom"}
        txt = synthesize_root_cause_for_sandbox_setup(diag)
        assert "boom" in txt


# ── Phase classification (P1-6 v2) ───────────────────────────────────────────


class TestPhaseClassification:
    """Classify the failing command into apt/pip/uv/git/infra/unknown."""

    @pytest.mark.parametrize("cmd,expected_phase,expected_subclass", [
        # apt
        ("apt-get install -y build-essential", "apt", "FAILED_SANDBOX_SETUP_APT"),
        ("DEBIAN_FRONTEND=noninteractive apt-get install -y git", "apt", "FAILED_SANDBOX_SETUP_APT"),
        # pip (but NOT uv pip)
        ("pip install -r requirements.txt", "pip", "FAILED_SANDBOX_SETUP_PIP"),
        ("pip3 install --upgrade pip", "pip", "FAILED_SANDBOX_SETUP_PIP"),
        # uv — must take precedence over pip
        ("uv pip install -e .", "uv", "FAILED_SANDBOX_SETUP_UV"),
        ("uv sync --frozen", "uv", "FAILED_SANDBOX_SETUP_UV"),
        ("uv run pytest", "uv", "FAILED_SANDBOX_SETUP_UV"),
        # git
        ("git clone https://github.com/foo/bar", "git", "FAILED_SANDBOX_SETUP_GIT"),
        ("git submodule update --init --recursive", "git", "FAILED_SANDBOX_SETUP_GIT"),
        # infra
        ("docker run -d --rm python:3.12-slim sleep infinity", "infra", "FAILED_SANDBOX_SETUP_UNKNOWN"),
        ("mkdir -p /workspace && chmod 777 /workspace", "infra", "FAILED_SANDBOX_SETUP_UNKNOWN"),
        # unknown
        ("./misc/trigger_wheel_build.sh", "unknown", "FAILED_SANDBOX_SETUP_UNKNOWN"),
        ("", "unknown", "FAILED_SANDBOX_SETUP_UNKNOWN"),
    ])
    def test_phase_classification(self, cmd, expected_phase, expected_subclass):
        from phalanx.shadow.runner import _classify_phase_from_command
        from phalanx.runtime.infra_verdicts import SANDBOX_PHASE_TO_FAILURE_CLASS
        phase = _classify_phase_from_command(cmd)
        assert phase == expected_phase, f"cmd={cmd!r} got {phase}"
        assert SANDBOX_PHASE_TO_FAILURE_CLASS[phase] == expected_subclass


# ── Detector with setup_log populated (apt/pip/uv/git failure shapes) ───────


class TestDetectorWithSetupLog:
    """When provision_on_the_fly fails AFTER some setup steps ran, the
    last setup_log entry has the failing step's command + exit_code +
    stderr. The detector pulls all of it."""

    def test_apt_install_failure(self):
        steps = [
            _setup_step(step="mkdir_workspace", cmd="mkdir -p /workspace", ok=True),
            _setup_step(step="docker_cp_workspace", cmd="docker cp", ok=True),
            _setup_step(
                step="apt_install_baseline",
                cmd="apt-get install -y build-essential git curl",
                ok=False, exit_code=100,
                stderr="E: Unable to locate package build-essential",
            ),
        ]
        task = _sre_failed_task(
            error_message="baseline_apt_install_failed_and_git_unavailable: E: Unable...",
            setup_log=steps,
        )
        diag = _detect_sre_setup_failure([task])
        assert diag is not None
        assert diag["phase"] == "apt"
        assert diag["failure_subclass"] == "FAILED_SANDBOX_SETUP_APT"
        assert diag["failed_step"] == "apt_install_baseline"
        assert "apt-get install" in diag["failed_command"]
        assert diag["exit_code"] == 100
        assert "Unable to locate" in diag["stderr_tail"]

    def test_pip_install_failure(self):
        steps = [
            _setup_step(step="mkdir_workspace", cmd="mkdir -p /workspace", ok=True),
            _setup_step(step="docker_cp_workspace", cmd="docker cp", ok=True),
            _setup_step(step="apt_install_baseline", cmd="apt-get install ...", ok=True),
            _setup_step(
                step="install_command",
                cmd="pip install -r requirements.txt",
                ok=False, exit_code=1,
                stderr="ERROR: Could not find a version that satisfies the requirement foo",
            ),
        ]
        task = _sre_failed_task(
            error_message="install_command_failed: pip install -r requirements.txt",
            setup_log=steps,
        )
        diag = _detect_sre_setup_failure([task])
        assert diag["phase"] == "pip"
        assert diag["failure_subclass"] == "FAILED_SANDBOX_SETUP_PIP"
        assert diag["exit_code"] == 1
        assert "Could not find a version" in diag["stderr_tail"]

    def test_uv_failure_not_misclassified_as_pip(self):
        steps = [
            _setup_step(
                step="install_command",
                cmd="uv pip install -e .",
                ok=False, exit_code=2,
                stderr="error: no matching distribution found",
            ),
        ]
        task = _sre_failed_task(
            error_message="install_command_failed: uv pip install -e .",
            setup_log=steps,
        )
        diag = _detect_sre_setup_failure([task])
        assert diag["phase"] == "uv", "uv pip must not classify as pip"
        assert diag["failure_subclass"] == "FAILED_SANDBOX_SETUP_UV"

    def test_git_submodule_failure(self):
        steps = [
            _setup_step(
                step="install_command",
                cmd="git submodule update --init --recursive",
                ok=False, exit_code=128,
                stderr="fatal: could not read Username for 'https://example.invalid'",
            ),
        ]
        task = _sre_failed_task(
            error_message="install_command_failed: git submodule update --init",
            setup_log=steps,
        )
        diag = _detect_sre_setup_failure([task])
        assert diag["phase"] == "git"
        assert diag["failure_subclass"] == "FAILED_SANDBOX_SETUP_GIT"
        assert diag["exit_code"] == 128

    def test_stdout_tail_propagated_when_present(self):
        steps = [
            _setup_step(
                step="install_command",
                cmd="pip install foo",
                ok=False, exit_code=1,
                stderr="boom",
                stdout="Collecting foo...",
            ),
        ]
        task = _sre_failed_task(
            error_message="install_command_failed: pip install foo",
            setup_log=steps,
        )
        diag = _detect_sre_setup_failure([task])
        assert diag["stdout_tail"] is not None
        assert "Collecting foo" in diag["stdout_tail"]


# ── Provenance carries the diagnostic ────────────────────────────────────────


class TestProvenanceCarriesDiagnostic:
    def test_sre_setup_diagnostic_field_present(self):
        diag = {"failed_step": "setup", "error_message": "boom", "base_image": "x"}
        prov = build_provenance([], sre_setup_diagnostic=diag)
        assert "sre_setup_diagnostic" in prov
        assert prov["sre_setup_diagnostic"] == diag

    def test_sre_setup_diagnostic_none_when_not_provided(self):
        prov = build_provenance([])
        assert prov["sre_setup_diagnostic"] is None

    def test_schema_version_bumped_to_2(self):
        """P1-6 added sre_setup_diagnostic — schema version MUST bump.
        v3: bumped again to 3 when verification_evidence was added."""
        assert PROVENANCE_SCHEMA_VERSION >= 2

    def test_provenance_schema_documents_diagnostic_key(self):
        """The keys-present test from P0-5 needs to include the new field."""
        prov = build_provenance([], sre_setup_diagnostic={"failed_step": "x"})
        # Required keys updated from P0-5.
        required = {
            "_schema_version", "chosen_source_role", "chosen_source_reason",
            "tl_task_id", "tl_task_created_at", "tl_task_sequence_num",
            "tl_task_status", "tl_task_confidence", "tl_task_review_decision",
            "tl_task_root_cause_head", "tl_task_count",
            "engineer_task_id", "engineer_task_status", "engineer_task_confidence",
            "root_cause_synthesized", "root_cause_synthesis_reason",
            "divergence_detected", "divergence_details",
            "sre_setup_diagnostic",  # NEW in v2
        }
        missing = required - set(prov.keys())
        assert not missing, f"provenance missing keys: {missing}"


# ── Backward compat: P0-5 schema-version test must NOT fail ──────────────────


class TestBackwardCompat:
    """The P0-5 test `test_schema_version_is_pinned` hardcoded EXPECTED_VERSION=1.
    That test needs updating in lockstep — but provenance.py code is what's
    canonical. This test asserts the runtime constant matches what the
    P0-5 contract was; if EXPECTED_VERSION needs to change, this is the
    audit trail for why."""

    def test_v2_is_strict_superset_of_v1(self):
        """v2 must include all v1 keys + sre_setup_diagnostic."""
        prov = build_provenance([])
        v1_required_keys = {
            "_schema_version", "chosen_source_role", "chosen_source_reason",
            "tl_task_id", "tl_task_created_at", "tl_task_sequence_num",
            "tl_task_status", "tl_task_confidence", "tl_task_review_decision",
            "tl_task_root_cause_head", "tl_task_count",
            "engineer_task_id", "engineer_task_status", "engineer_task_confidence",
            "root_cause_synthesized", "root_cause_synthesis_reason",
            "divergence_detected", "divergence_details",
        }
        for k in v1_required_keys:
            assert k in prov, f"v2 dropped v1 key: {k}"
