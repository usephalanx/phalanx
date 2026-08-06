"""P0-4 — fresh-DB migration CI: static checks on the workflow + scripts.

End-to-end verification is `make migration-check` which requires Docker.
This file covers the cheap checks that should run on every PR regardless:

  - workflow YAML parses and has the expected trigger paths
  - workflow has the three required alembic steps + the schema diff step
  - the schema-fingerprint script is executable + parses
  - the local-parity script is executable + parses
  - the local-parity script exercises the same three commands as CI
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "migration-bootstrap.yml"
FINGERPRINT_SH = REPO_ROOT / "scripts" / "migration_schema_fingerprint.sh"
LOCAL_CHECK_SH = REPO_ROOT / "scripts" / "migration_bootstrap_check.sh"


# ── Workflow file ─────────────────────────────────────────────────────────────


class TestWorkflowFile:
    def test_workflow_exists(self):
        assert WORKFLOW.exists()

    def test_workflow_parses_as_yaml(self):
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        assert data["name"] == "Migration bootstrap (P0-4)"

    def test_triggers_on_pr_paths(self):
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        # YAML's `on:` parses to True (Python bool) without quoting — handle both.
        triggers = data.get("on") or data.get(True)
        assert "pull_request" in triggers
        paths = triggers["pull_request"]["paths"]
        # Required by P0-4 spec.
        assert any("alembic/**" in p for p in paths)
        assert any("phalanx/db/**" in p for p in paths)

    def test_postgres_service_uses_pgvector(self):
        """Plain postgres would fail `CREATE EXTENSION vector` in the initial
        migration. The service image must include pgvector."""
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        job = data["jobs"]["fresh-db-migrations"]
        assert "pgvector" in job["services"]["postgres"]["image"]

    def test_database_url_uses_localhost(self):
        """GH Actions services bind to localhost on the runner. The alembic
        env.py reads DATABASE_URL — must point at localhost, not 'postgres'."""
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        env = data["jobs"]["fresh-db-migrations"]["env"]
        assert "@localhost:5432" in env["DATABASE_URL"]

    def test_has_three_alembic_steps(self):
        """The whole point: upgrade head → downgrade base → upgrade head."""
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        run_text = " ".join(
            s.get("run", "") for s in data["jobs"]["fresh-db-migrations"]["steps"]
        )
        # First fresh upgrade
        assert run_text.count("alembic -c alembic/alembic.ini upgrade head") >= 2, (
            "expected at least 2 'upgrade head' steps (fresh + round-trip)"
        )
        assert "alembic -c alembic/alembic.ini downgrade base" in run_text

    def test_has_schema_fingerprint_diff_step(self):
        """The job must compare fresh-install vs round-trip schemas."""
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        steps = data["jobs"]["fresh-db-migrations"]["steps"]
        step_names = [s.get("name", "") for s in steps]
        assert any("fingerprint" in n.lower() for n in step_names), (
            f"no fingerprint step in {step_names}"
        )

    def test_uploads_fingerprints_on_failure(self):
        """When the diff fails, the operator needs the artifacts to triage."""
        with WORKFLOW.open() as f:
            data = yaml.safe_load(f)
        steps = data["jobs"]["fresh-db-migrations"]["steps"]
        upload_steps = [
            s for s in steps
            if s.get("uses", "").startswith("actions/upload-artifact")
        ]
        assert upload_steps, "no upload-artifact step found"
        assert any(s.get("if") == "always()" for s in upload_steps), (
            "upload step must run on failure (if: always())"
        )


# ── Scripts ───────────────────────────────────────────────────────────────────


class TestScriptsParse:
    @pytest.mark.parametrize("path", [FINGERPRINT_SH, LOCAL_CHECK_SH])
    def test_executable(self, path):
        mode = path.stat().st_mode
        assert mode & 0o111, f"{path} is not executable"

    @pytest.mark.parametrize("path", [FINGERPRINT_SH, LOCAL_CHECK_SH])
    def test_bash_parses(self, path):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestLocalParity:
    """The local-parity script must exercise the SAME three alembic commands
    the CI workflow runs. Otherwise the local run gives false confidence."""

    LOCAL_SCRIPT_TEXT = (
        LOCAL_CHECK_SH.read_text() if LOCAL_CHECK_SH.exists() else ""
    )

    def test_runs_upgrade_head_at_least_twice(self):
        n = self.LOCAL_SCRIPT_TEXT.count("alembic -c alembic/alembic.ini upgrade head")
        assert n >= 2, f"expected ≥2 upgrade-head calls, found {n}"

    def test_runs_downgrade_base(self):
        assert "alembic -c alembic/alembic.ini downgrade base" in self.LOCAL_SCRIPT_TEXT

    def test_uses_throwaway_container(self):
        """Must not point at forge-postgres — would wipe the live ledger."""
        assert "phalanx-migration-check-" in self.LOCAL_SCRIPT_TEXT
        assert "trap cleanup EXIT" in self.LOCAL_SCRIPT_TEXT
        # No code path overrides PHALANX_PG_TARGET to forge-postgres.
        assert "forge-postgres" not in re.sub(
            r"#.*", "", self.LOCAL_SCRIPT_TEXT  # strip comments
        )

    def test_diffs_fingerprints(self):
        """Local check must compare fresh vs round-trip — same as CI."""
        assert "diff -u /tmp/fingerprint-fresh.txt /tmp/fingerprint-roundtrip.txt" \
            in self.LOCAL_SCRIPT_TEXT


# ── Fingerprint output shape ──────────────────────────────────────────────────


class TestFingerprintScriptShape:
    """The fingerprint script's output must be stable across runs of the
    same schema (no timestamps, no row counts, no auto-id leakage)."""

    SCRIPT_TEXT = (
        FINGERPRINT_SH.read_text() if FINGERPRINT_SH.exists() else ""
    )

    def test_excludes_alembic_version_table(self):
        """alembic_version stores the current head — would differ between
        upgrade and downgrade points if included."""
        assert "alembic_version" in self.SCRIPT_TEXT
        # Specifically as an exclusion.
        assert "!= 'alembic_version'" in self.SCRIPT_TEXT

    def test_reads_information_schema_columns(self):
        """Uses the standard catalog so the output is identical to what
        any other inspection tool would produce."""
        assert "information_schema.columns" in self.SCRIPT_TEXT

    def test_orders_deterministically(self):
        """Must sort by (table, column) — otherwise the diff is noisy."""
        assert "ORDER BY c.table_name, c.column_name" in self.SCRIPT_TEXT


# ── Makefile wiring ───────────────────────────────────────────────────────────


class TestMakefileTarget:
    def test_migration_check_target_present(self):
        mk = (REPO_ROOT / "Makefile").read_text()
        assert "migration-check:" in mk
        assert "scripts/migration_bootstrap_check.sh" in mk
