"""P0-3 — preflight check: data-loss prevention before `docker compose up`.

The preflight script is bash. We test it through subprocess by:
  - building a temp repo root (with docker-compose.yml + .env) per scenario
  - using PHALANX_TEST_* env vars (the script's test seams) to stub docker
  - asserting exit code and reported failure text

Exit-code contract:
  0  passed
  1  hard refusal — fix required (.env missing, docker down, etc.)
  2  soft refusal — overridable via documented env flag
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "preflight_check.sh"
EXPECTED_VOLUME = "phalanx-dev_forge-postgres-data"
COMPOSE_NAME = "phalanx-dev"


def _make_temp_repo(tmp_path: Path, *, with_env: bool = True, name_value: str = COMPOSE_NAME,
                   db_url: str | None = "postgresql+asyncpg://forge:forge_dev_password@postgres:5432/forge"
                   ) -> Path:
    """Build a minimal temp 'repo' (docker-compose.yml + .env) the preflight
    can consume. Returns the temp root."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(textwrap.dedent(f"""
        name: {name_value}
        services:
          postgres:
            image: pgvector/pgvector:pg16
    """).lstrip())
    if with_env:
        env_lines = []
        if db_url is not None:
            env_lines.append(f"DATABASE_URL={db_url}")
        (tmp_path / ".env").write_text("\n".join(env_lines) + "\n")
    # Symlink in the real preflight script so it can find itself by repo-root.
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "preflight_check.sh")
    (tmp_path / "scripts" / "preflight_check.sh").chmod(0o755)
    return tmp_path


def _run(repo: Path, **env_overrides) -> subprocess.CompletedProcess:
    """Invoke the preflight against a temp repo with controlled env."""
    env = {
        # Keep PATH so docker (if called as fallback) is findable.
        "PATH": os.environ.get("PATH", ""),
        "NO_COLOR": "1",
        "PHALANX_TEST_REPO_ROOT": str(repo),
        "PHALANX_TEST_DOCKER_INFO_OK": "1",
        # Default: pretend the expected volume exists.
        "PHALANX_TEST_VOLUMES": EXPECTED_VOLUME,
        "PHALANX_TEST_VOLUME_CREATED_AT": "2026-05-05T12:00:00Z",
    }
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    # Allow callers to delete a key by passing value=__DELETE__
    for k in list(env.keys()):
        if env[k] == "__DELETE__":
            del env[k]
    return subprocess.run(
        ["bash", str(repo / "scripts" / "preflight_check.sh")],
        capture_output=True, text=True, env=env,
    )


# ── Happy path ────────────────────────────────────────────────────────────────


class TestPasses:
    def test_normal_existing_volume_passes(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "All preflight checks passed" in result.stdout

    def test_volume_age_is_surfaced(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        result = _run(repo, PHALANX_TEST_VOLUME_CREATED_AT="2026-05-04T12:00:00Z")
        assert result.returncode == 0
        # Should show created date + a humanized age suffix (d / h / min)
        assert "created" in result.stdout
        assert "2026-05-04T12:00:00Z" in result.stdout


# ── Hard refusals (exit 1) ────────────────────────────────────────────────────


class TestHardRefusals:
    def test_missing_env_refuses(self, tmp_path):
        repo = _make_temp_repo(tmp_path, with_env=False)
        result = _run(repo)
        assert result.returncode == 1
        assert ".env is missing" in result.stdout
        assert "cp .env.example .env" in result.stdout

    def test_wrong_compose_project_name_refuses(self, tmp_path):
        repo = _make_temp_repo(tmp_path, name_value="something-else")
        result = _run(repo)
        assert result.returncode == 1
        assert "compose project name mismatch" in result.stdout
        assert "DATA LOSS RISK" in result.stdout

    def test_docker_daemon_down_refuses(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        result = _run(repo, PHALANX_TEST_DOCKER_INFO_OK="0")
        assert result.returncode == 1
        assert "docker daemon is not running" in result.stdout
        assert "start Docker Desktop" in result.stdout

    def test_unwritable_ledger_path_refuses(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        # A path whose parent we can't create or write.
        unwritable = "/proc/cannot-write/ledger.jsonl"
        result = _run(repo, PHALANX_LEDGER_JSONL_PATH=unwritable)
        assert result.returncode == 1
        assert "ledger.jsonl path is not writable" in result.stdout
        assert "P0-2 export would silently fail" in result.stdout


# ── Soft refusals (exit 2 — overridable) ──────────────────────────────────────


class TestSoftRefusals:
    def test_missing_volume_refuses_without_flag(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        result = _run(repo, PHALANX_TEST_VOLUMES="other,redis-data")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "postgres volume not found" in result.stdout
        assert "DATA LOSS RISK" in result.stdout
        assert "PHALANX_ALLOW_FRESH_BOOT=1" in result.stdout

    def test_missing_volume_allowed_with_fresh_boot(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        result = _run(
            repo,
            PHALANX_TEST_VOLUMES="other,redis-data",
            PHALANX_ALLOW_FRESH_BOOT="1",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The warning must explicitly tell the operator what to do post-boot.
        assert "MISSING — allowed" in result.stdout
        assert "make backup" in result.stdout
        assert "make ledger-verify" in result.stdout

    def test_external_database_url_refuses_without_flag(self, tmp_path):
        repo = _make_temp_repo(
            tmp_path,
            db_url="postgresql+asyncpg://forge:secret@some.cloud.host:5432/forge",
        )
        result = _run(repo)
        assert result.returncode == 2
        assert "DATABASE_URL does not point to compose-internal postgres" in result.stdout
        # Password must be redacted in the diagnostic output.
        assert "secret" not in result.stdout
        assert "USER:PASS" in result.stdout

    def test_external_database_url_allowed_with_flag(self, tmp_path):
        repo = _make_temp_repo(
            tmp_path,
            db_url="postgresql+asyncpg://forge:secret@some.cloud.host:5432/forge",
        )
        result = _run(repo, PHALANX_ALLOW_EXTERNAL_DB="1")
        assert result.returncode == 0
        assert "allowed by PHALANX_ALLOW_EXTERNAL_DB" in result.stdout
        assert "secret" not in result.stdout


# ── Escape hatch ──────────────────────────────────────────────────────────────


class TestSkipFlag:
    def test_skip_preflight_bypasses_all_checks(self, tmp_path):
        # Set up a repo that would otherwise fail HARD (.env missing).
        repo = _make_temp_repo(tmp_path, with_env=False)
        result = _run(repo, PHALANX_SKIP_PREFLIGHT="1")
        assert result.returncode == 0
        assert "preflight skipped" in result.stderr or "preflight skipped" in result.stdout


# ── Warnings ──────────────────────────────────────────────────────────────────


class TestVolumeAgeWarning:
    def test_very_new_volume_emits_warning(self, tmp_path):
        """A volume created seconds ago is suspicious — surface it."""
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo = _make_temp_repo(tmp_path)
        result = _run(repo, PHALANX_TEST_VOLUME_CREATED_AT=recent)
        assert result.returncode == 0
        assert "very new" in result.stdout or "Preflight passed with 1 warning" in result.stdout


# ── Diagnostic quality ────────────────────────────────────────────────────────


class TestDiagnosticOutput:
    """The script's job is partly to teach the operator what to do next."""

    def test_failure_shows_exact_override_command(self, tmp_path):
        repo = _make_temp_repo(tmp_path)
        result = _run(repo, PHALANX_TEST_VOLUMES="other")
        # The hint must give the *exact* override command, not vague text.
        assert "PHALANX_ALLOW_FRESH_BOOT=1 make up" in result.stdout

    def test_failure_does_not_run_docker_compose(self, tmp_path):
        repo = _make_temp_repo(tmp_path, with_env=False)
        result = _run(repo)
        assert "docker compose up will NOT run" in result.stdout

    def test_db_url_password_is_redacted_on_failure(self, tmp_path):
        repo = _make_temp_repo(
            tmp_path, db_url="postgresql+asyncpg://forge:super_secret_pw@elsewhere/forge"
        )
        result = _run(repo)
        assert "super_secret_pw" not in result.stdout
        assert "super_secret_pw" not in result.stderr
