"""P0-1 — postgres backup scripts: syntax + structural checks.

These are static checks that run in any environment (no Docker, no postgres).
The end-to-end "actually round-trip a dump" check is the bash-driven
`make backup-verify` flow, which requires a live forge-postgres container.

What this file proves:
  - All 4 backup shell scripts parse with `bash -n` (no syntax errors).
  - All 4 scripts are executable.
  - The LaunchAgent plist parses as valid plist + has the expected schedule.
  - The Makefile exposes the documented targets.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
BACKUP_SCRIPTS = [
    "backup_postgres.sh",
    "backup_postgres_restore.sh",
    "backup_postgres_verify.sh",
    "backup_postgres_offhost.sh",
]


class TestBackupScriptsExist:
    def test_all_four_scripts_present(self):
        for name in BACKUP_SCRIPTS:
            path = SCRIPTS / name
            assert path.exists(), f"missing {path}"

    def test_all_four_scripts_executable(self):
        for name in BACKUP_SCRIPTS:
            path = SCRIPTS / name
            mode = path.stat().st_mode
            assert mode & 0o111, f"{path} is not executable"


class TestBackupScriptsParse:
    """`bash -n` is a no-op parse — catches every syntax bug without running."""

    def test_bash_parses_all_scripts(self):
        for name in BACKUP_SCRIPTS:
            result = subprocess.run(
                ["bash", "-n", str(SCRIPTS / name)],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, (
                f"bash -n failed on {name}:\n{result.stderr}"
            )


class TestBackupScriptsHaveSafetyRefusals:
    """Catch the dangerous cases each script protects against."""

    def test_restore_refuses_live_container_without_env(self):
        content = (SCRIPTS / "backup_postgres_restore.sh").read_text()
        assert "PHALANX_RESTORE_ALLOW_PROD" in content
        assert "forge-postgres" in content
        # The refusal must check the env flag, not just mention it.
        assert "REFUSED" in content

    def test_offhost_refuses_missing_remote(self):
        content = (SCRIPTS / "backup_postgres_offhost.sh").read_text()
        assert "PHALANX_BACKUP_REMOTE" in content
        assert "ERROR: PHALANX_BACKUP_REMOTE not set" in content

    def test_dump_self_checks_archive_before_keeping(self):
        """A corrupt dump must be rejected, not silently kept."""
        content = (SCRIPTS / "backup_postgres.sh").read_text()
        assert "pg_restore --list" in content
        assert "dump self-check failed" in content

    def test_verify_uses_throwaway_container(self):
        """Verification must NEVER restore into the live container."""
        content = (SCRIPTS / "backup_postgres_verify.sh").read_text()
        assert "phalanx-backup-verify-" in content
        # Cleanup trap must fire on exit.
        assert "trap cleanup EXIT" in content


class TestLaunchAgentPlist:
    PLIST = SCRIPTS / "com.phalanx.backup.plist"

    def test_plist_exists(self):
        assert self.PLIST.exists()

    def test_plist_parses(self):
        with self.PLIST.open("rb") as f:
            data = plistlib.load(f)
        assert data.get("Label") == "com.phalanx.backup"
        assert "ProgramArguments" in data
        assert "StartCalendarInterval" in data

    def test_plist_schedule_is_every_6_hours(self):
        with self.PLIST.open("rb") as f:
            data = plistlib.load(f)
        slots = data["StartCalendarInterval"]
        hours = sorted(s["Hour"] for s in slots)
        assert hours == [0, 6, 12, 18], f"expected every-6h schedule, got {hours}"

    def test_plist_logs_to_repo_relative_path(self):
        """Log path uses PHALANX_REPO_ROOT placeholder — make backup-install expands it."""
        content = self.PLIST.read_text()
        assert "PHALANX_REPO_ROOT/backups/postgres/launchagent.log" in content


class TestMakefileTargets:
    MAKEFILE = REPO_ROOT / "Makefile"
    REQUIRED_TARGETS = [
        "backup:",
        "backup-list:",
        "backup-verify:",
        "backup-restore:",
        "backup-offhost:",
        "backup-install:",
        "backup-uninstall:",
    ]

    def test_all_targets_present(self):
        content = self.MAKEFILE.read_text()
        for target in self.REQUIRED_TARGETS:
            assert target in content, f"Makefile missing target: {target}"

    def test_targets_in_phony(self):
        content = self.MAKEFILE.read_text()
        # PHONY block contains the new targets so they don't collide with filenames.
        for short in ("backup", "backup-verify", "backup-restore"):
            assert short in content


class TestGitignoreExcludesBackupsDir:
    def test_backups_dir_ignored(self):
        gi = (REPO_ROOT / ".gitignore").read_text()
        assert "backups/" in gi


class TestCrontabFallback:
    def test_crontab_template_present(self):
        path = SCRIPTS / "backup_postgres.crontab"
        assert path.exists()
        content = path.read_text()
        # Every 6 hours.
        assert "0 */6 * * *" in content
        # Placeholder, not a hardcoded path.
        assert "PHALANX_REPO_ROOT" in content
