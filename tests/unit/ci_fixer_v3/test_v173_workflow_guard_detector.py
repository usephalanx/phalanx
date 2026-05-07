"""v1.7.3 post-Phase-2a — workflow guard-command detector tests.

Phase 2a entries E2 (psf/black) and E5 (sphinx-doc/sphinx) hit
sandbox_provisioning_failed because the SRE setup ran workflow `run:`
steps that are CI-only guards — `if [ "$GITHUB_BASE_REF" != "main" ];
then exit 1; fi` and similar. These should NEVER execute in our
sandbox.

Tests cover:
  - Real shapes from psf/black + sphinx-doc/sphinx that triggered E2/E5
  - Synthetic guards (GITHUB_HEAD_REF, GHA annotation echo, etc.)
  - Defensive false-positive checks: install scripts that legitimately
    use exit codes or env vars must NOT be flagged
  - End-to-end: workflow with a guard step → recipe.commands skips it,
    recipe.skipped_guard_commands captures it
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from phalanx.agents._v171_workflow_extractor import (
    _is_workflow_guard_command,
    extract_recipe_from_workflow,
)


# ── Real-world guard shapes from Phase 2a ─────────────────────────────


class TestRealWorldGuardShapes:
    def test_psf_black_pr_target_check(self):
        """psf/black lint workflow's first step — fails E2 in Phase 2a."""
        cmd = (
            'if [ "$GITHUB_BASE_REF" != "main" ]; then\n'
            '    echo "::error::PR targeting \'$GITHUB_BASE_REF\', '
            'please refile targeting \'main\'." && exit 1\n'
            'fi'
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_sphinx_event_name_gate(self):
        """Sphinx-style: gate on event name (synthetic but plausible
        shape — sphinx had a different failure that hit an `exit 1`
        path; this proves the same detector covers GITHUB_EVENT_NAME
        gates too)."""
        cmd = (
            'if [ "$GITHUB_EVENT_NAME" = "schedule" ]; then\n'
            '    echo "Skipping scheduled run on this branch"\n'
            '    exit 0\n'
            'fi'
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_inverse_pr_target_one_liner(self):
        """Same idea, one-line || form."""
        cmd = '[ "$GITHUB_BASE_REF" = "main" ] || exit 1'
        assert _is_workflow_guard_command(cmd) is True

    def test_head_ref_regex_gate(self):
        """Bot-only branch enforcement — common in dependabot setups."""
        cmd = '[[ "$GITHUB_HEAD_REF" =~ ^bot/.* ]] || exit 1'
        assert _is_workflow_guard_command(cmd) is True

    def test_gha_annotation_with_exit(self):
        """GHA `::error::` workflow command + exit — unambiguously a
        runner-only gate even when no GHA env var is referenced
        directly."""
        cmd = 'echo "::error::Wrong base branch"; exit 1'
        assert _is_workflow_guard_command(cmd) is True

    def test_gha_warning_annotation_with_exit(self):
        """`::warning::` annotation also indicates GHA-runner-only logic."""
        cmd = 'echo "::warning::Suspicious file"; exit 2'
        assert _is_workflow_guard_command(cmd) is True


# ── False-positive defenses ───────────────────────────────────────────


class TestFalsePositiveDefenses:
    def test_pip_install_is_not_a_guard(self):
        cmd = 'pip install -e ".[tests]"'
        assert _is_workflow_guard_command(cmd) is False

    def test_pytest_invocation_is_not_a_guard(self):
        cmd = "pytest -v tests/"
        assert _is_workflow_guard_command(cmd) is False

    def test_install_script_with_exit_but_no_gha_var_is_not_a_guard(self):
        """Real installers sometimes early-exit (pip install || exit 1).
        Without a GHA-only env var, this stays in install_commands."""
        cmd = "pip install -r requirements.txt || exit 1"
        assert _is_workflow_guard_command(cmd) is False

    def test_command_referencing_github_workspace_is_not_a_guard(self):
        """GITHUB_WORKSPACE is set in the sandbox — using it doesn't
        make the command a guard. Also: no exit."""
        cmd = "ls $GITHUB_WORKSPACE"
        assert _is_workflow_guard_command(cmd) is False

    def test_github_token_alone_is_not_a_guard(self):
        """We deliberately don't flag GITHUB_TOKEN — it's commonly used
        in legitimate install commands (private repo fetches, GH releases)."""
        cmd = (
            "curl -sSL -H 'Authorization: token $GITHUB_TOKEN' "
            "https://example.com/install.sh | sh"
        )
        assert _is_workflow_guard_command(cmd) is False

    def test_exit_zero_in_install_path_is_not_a_guard(self):
        """`|| exit 0` patterns in install scripts (e.g., 'cleanup
        cache; rm -rf x || exit 0') aren't guards — no GHA env var."""
        cmd = "rm -rf .pytest_cache || exit 0"
        assert _is_workflow_guard_command(cmd) is False

    def test_empty_command_is_not_a_guard(self):
        assert _is_workflow_guard_command("") is False
        assert _is_workflow_guard_command(None) is False  # type: ignore[arg-type]


# ── End-to-end: extract_recipe_from_workflow handles guards ───────────


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / ".github" / "workflows").mkdir(parents=True)
        yield ws


def _write_workflow(ws: Path, name: str, content: str) -> Path:
    path = ws / ".github" / "workflows" / name
    path.write_text(textwrap.dedent(content))
    return path


class TestExtractRecipeWithGuards:
    def test_psf_black_style_workflow_skips_guard(self, workspace):
        """End-to-end: workflow with a PR-target guard step + a real
        install step. Guard goes to skipped_guard_commands; install
        stays in commands. The recipe is still usable (commands non-empty)."""
        wf = _write_workflow(
            workspace,
            "lint.yml",
            """\
            name: lint
            on: pull_request
            jobs:
              lint:
                name: lint
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: actions/setup-python@v5
                    with:
                      python-version: '3.12'
                  - name: Check PR target
                    run: |
                      if [ "$GITHUB_BASE_REF" != "main" ]; then
                          echo "::error::Wrong target" && exit 1
                      fi
                  - name: Install
                    run: pip install -e ".[lint]"
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "lint")
        assert recipe is not None
        # The guard is NOT in install commands
        assert not any('GITHUB_BASE_REF' in c for c in recipe.commands)
        # But it IS recorded as evidence
        assert len(recipe.skipped_guard_commands) == 1
        assert 'GITHUB_BASE_REF' in recipe.skipped_guard_commands[0]
        # The real install command survives
        assert any('pip install' in c for c in recipe.commands)

    def test_workflow_with_only_guard_step_produces_empty_install(self, workspace):
        """Edge case: workflow whose only `run:` step is a guard.
        Recipe.commands ends up empty (apart from any uses-handler
        outputs). Caller should be able to detect this and fall to
        Tier 1 if needed."""
        wf = _write_workflow(
            workspace,
            "guard-only.yml",
            """\
            name: guard
            on: pull_request
            jobs:
              guard:
                name: guard
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: '[ "$GITHUB_BASE_REF" = "main" ] || exit 1'
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "guard")
        assert recipe is not None
        assert recipe.commands == []
        assert recipe.skipped_guard_commands == [
            '[ "$GITHUB_BASE_REF" = "main" ] || exit 1'
        ]

    def test_workflow_without_guards_gets_empty_skipped_list(self, workspace):
        """Non-guard workflows produce skipped_guard_commands == []."""
        wf = _write_workflow(
            workspace,
            "test.yml",
            """\
            name: test
            on: pull_request
            jobs:
              test:
                name: test
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: pip install -e .
                  - run: pytest
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "test")
        assert recipe is not None
        assert recipe.skipped_guard_commands == []
        # pytest is also skipped — but as test runner, not guard
        assert any('pip install' in c for c in recipe.commands)
        assert not any('pytest' in c for c in recipe.commands)


# ── ExtractedRecipe shape stability ───────────────────────────────────


class TestRecipeShape:
    def test_recipe_has_skipped_guard_commands_field(self, workspace):
        """The new field defaults to [] for workflows without guards
        — backward-compat with any consumer that introspects the
        recipe dataclass."""
        from phalanx.agents._v171_workflow_extractor import ExtractedRecipe

        recipe = ExtractedRecipe(
            workflow_file=".github/workflows/x.yml",
            job_key="test",
            job_name="test",
            runs_on="ubuntu-latest",
        )
        assert recipe.skipped_guard_commands == []
